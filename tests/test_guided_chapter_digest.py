from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from uuid6 import uuid7

from virtual_self.auth.credentials import resolve_auth
from virtual_self.config import get_settings
from virtual_self.persistence.db import connection, execute, fetch_all, fetch_one
from virtual_self.persistence.migrate import apply_migrations
from virtual_self.services.guided_chapter_digest import (
    ChapterTarget,
    MemorySnippet,
    load_digest_summary,
    run_chapter_guided_digests,
    synthesize_chapter_digest,
)
from virtual_self.services.guided_digest_prompts import GUIDED_QUESTIONS


def _insert_dated_memory(conn, auth, *, content: str, when: datetime):
    mem_id = uuid7()
    ver_id = uuid7()
    execute(
        conn,
        """
        INSERT INTO artifacts (id, artifact_type, subject_id, sensitivity)
        VALUES (%s, 'memory', %s, 1)
        """,
        (mem_id, auth.subject_id),
    )
    execute(
        conn,
        """
        INSERT INTO memories (
          id, subject_id, kind, scope, time_precision, valid_from, authored_at
        ) VALUES (%s, %s, 'fact', 'chat', 'day', %s, %s)
        """,
        (mem_id, auth.subject_id, when, when),
    )
    execute(
        conn,
        """
        INSERT INTO artifacts (id, artifact_type, subject_id, sensitivity)
        VALUES (%s, 'memory_version', %s, 1)
        """,
        (ver_id, auth.subject_id),
    )
    execute(
        conn,
        """
        INSERT INTO memory_versions (
          id, memory_id, version, content, status
        ) VALUES (%s, %s, 1, %s, 'active')
        """,
        (ver_id, mem_id, content),
    )
    execute(
        conn,
        "UPDATE memories SET current_version_id = %s WHERE id = %s",
        (ver_id, mem_id),
    )
    return mem_id


def _insert_proposed_chapter(conn, auth, *, title: str, memory_id: UUID):
    strand = fetch_one(conn, "SELECT id FROM life_strands WHERE slug = 'place'")
    chapter_id = uuid7()
    execute(
        conn,
        """
        INSERT INTO life_chapters (
          id, subject_id, strand_id, title, summary, status,
          valid_from, valid_to, time_precision,
          valid_from_precision, valid_to_precision, edge_fuzz_days
        ) VALUES (
          %s, %s, %s, %s, %s, 'proposed',
          %s, NULL, 'month', 'month', 'unknown', 0
        )
        """,
        (
            chapter_id,
            auth.subject_id,
            strand["id"],
            title,
            "test chapter",
            datetime(2024, 1, 1, tzinfo=timezone.utc),
        ),
    )
    execute(
        conn,
        """
        INSERT INTO life_chapter_evidence (id, chapter_id, memory_id, relationship)
        VALUES (%s, %s, %s, 'supporting')
        """,
        (uuid7(), chapter_id, memory_id),
    )
    return chapter_id


def test_guided_questions_cover_all_layers():
    assert set(GUIDED_QUESTIONS) == {"values", "voice", "decision_style", "modes"}


def test_synthesize_deterministic_cites_memory_ids(ensure_db):
    apply_migrations()
    mid = uuid7()
    chapter = ChapterTarget(
        chapter_id=uuid7(),
        title="New city",
        summary="moved",
        strand_slug="place",
        valid_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
        valid_to=None,
        status="proposed",
    )
    snippets = [
        MemorySnippet(
            memory_id=mid,
            content="Moved to Pune after the breakup and started therapy journaling.",
            register="journal",
            scope="chat",
        )
    ]
    token = get_settings().bootstrap_token
    auth = resolve_auth(f"Bearer {token.get_secret_value()}")
    out = synthesize_chapter_digest(auth, chapter, snippets, use_llm=False)
    assert out["guided"]
    assert out["profile"]
    for row in out["guided"]:
        assert row["memory_ids"] == [str(mid)]
    for row in out["profile"]:
        assert row["memory_ids"] == [str(mid)]


def test_run_persists_draft_digest_with_evidence(ensure_db):
    apply_migrations()
    get_settings.cache_clear()
    token = get_settings().bootstrap_token
    auth = resolve_auth(f"Bearer {token.get_secret_value()}")

    chapter_id = None
    try:
        with connection() as conn:
            execute(
                conn,
                """
                DELETE FROM life_chapter_guided_digests
                WHERE subject_id = %s
                """,
                (auth.subject_id,),
            )
            when = datetime(2024, 6, 1, tzinfo=timezone.utc)
            mid = _insert_dated_memory(
                conn,
                auth,
                content="Moved to a new city; grieving but building homelab projects.",
                when=when,
            )
            chapter_id = _insert_proposed_chapter(
                conn, auth, title="TR2 digest city move", memory_id=mid
            )
            conn.commit()

        report = run_chapter_guided_digests(
            auth,
            dry_run=False,
            use_llm=False,
            digest_status="draft",
            replace_existing=True,
            max_chapters=50,
        )
        assert report.digests_written >= 1
        hit = next(d for d in report.digests if d["chapter_id"] == str(chapter_id))
        digest_id = UUID(hit["digest_id"])

        with connection() as conn:
            row = fetch_one(
                conn,
                "SELECT status FROM life_chapter_guided_digests WHERE id = %s",
                (digest_id,),
            )
            assert row["status"] == "draft"
            summary = load_digest_summary(conn, digest_id)
        profile_rows = [i for i in summary["items"] if i["step"] == "profile_sentence"]
        assert profile_rows
        assert all(i["memory_ids"] for i in profile_rows)
        assert all(str(mid) in i["memory_ids"] for i in profile_rows)
    finally:
        if chapter_id is not None:
            with connection() as conn:
                execute(
                    conn,
                    "DELETE FROM life_chapter_guided_digests WHERE chapter_id = %s",
                    (chapter_id,),
                )
                execute(
                    conn,
                    "DELETE FROM life_chapter_evidence WHERE chapter_id = %s",
                    (chapter_id,),
                )
                execute(conn, "DELETE FROM life_chapters WHERE id = %s", (chapter_id,))
                conn.commit()


def test_migration_013_guided_digest_tables(ensure_db):
    apply_migrations()
    with connection() as conn:
        for table in (
            "life_chapter_guided_digests",
            "life_chapter_guided_digest_items",
            "life_chapter_guided_digest_evidence",
        ):
            row = fetch_one(
                conn,
                """
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = %s
                """,
                (table,),
            )
            assert row is not None
