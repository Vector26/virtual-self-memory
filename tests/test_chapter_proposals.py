from __future__ import annotations

from datetime import datetime, timezone
from uuid6 import uuid7

from virtual_self.auth.credentials import resolve_auth
from virtual_self.config import get_settings
from virtual_self.persistence.db import connection, execute, fetch_all, fetch_one
from virtual_self.persistence.migrate import apply_migrations
from virtual_self.services.chapter_proposals import (
    EpisodeRow,
    build_chapter_drafts,
    run_chapter_proposals,
    score_strand,
)


def test_score_strand_keywords():
    assert score_strand("After the breakup I started therapy") == "relationships"
    assert score_strand("We moved to a new city last year") == "place"


def test_build_drafts_detects_known_patterns():
    eps = [
        EpisodeRow(
            uuid7(),
            "dating through college and school years",
            datetime(2018, 6, 1, tzinfo=timezone.utc),
            "year",
            "imported",
        ),
        EpisodeRow(
            uuid7(),
            "still together during college",
            datetime(2020, 3, 1, tzinfo=timezone.utc),
            "year",
            "imported",
        ),
        EpisodeRow(
            uuid7(),
            "post-breakup rebuild journaling",
            datetime(2023, 1, 1, tzinfo=timezone.utc),
            "year",
            "imported",
        ),
        EpisodeRow(
            uuid7(),
            "moved to another city for work",
            datetime(2024, 5, 1, tzinfo=timezone.utc),
            "month",
            "imported",
        ),
    ]
    drafts = build_chapter_drafts(eps, use_llm=False, gap_days=400, max_proposals=15)
    titles = " | ".join(d.title.lower() for d in drafts)
    assert "breakup" in titles or "rebuild" in titles
    assert "city" in titles


def _insert_dated_memory(conn, auth, *, content: str, when: datetime, precision: str = "day"):
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
        ) VALUES (%s, %s, 'fact', 'chat', %s, %s, %s)
        """,
        (mem_id, auth.subject_id, precision, when, when),
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


def test_run_chapter_proposals_persists_proposed_with_evidence(ensure_db):
    apply_migrations()
    get_settings.cache_clear()
    token = get_settings().bootstrap_token
    auth = resolve_auth(f"Bearer {token.get_secret_value()}")

    with connection() as conn:
        execute(
            conn,
            """
            DELETE FROM life_chapter_evidence
            WHERE chapter_id IN (
              SELECT id FROM life_chapters
              WHERE subject_id = %s AND status = 'proposed'
            )
            """,
            (auth.subject_id,),
        )
        execute(
            conn,
            "DELETE FROM life_chapters WHERE subject_id = %s AND status = 'proposed'",
            (auth.subject_id,),
        )
        # Earlier runs leave identical 2099 memories; evidence is capped, so they crowd out this run's.
        execute(
            conn,
            """
            UPDATE artifacts SET deleted_at = NOW()
            WHERE deleted_at IS NULL AND id IN (
              SELECT m.id FROM memories m
              JOIN memory_versions mv ON mv.id = m.current_version_id
              WHERE m.subject_id = %s AND m.valid_from >= '2099-01-01'
                AND mv.content IN (
                  'moved to a new city after the breakup',
                  'first week in the new city, still unpacking'
                )
            )
            """,
            (auth.subject_id,),
        )
        conn.commit()

    # Isolated era (2099) so dedupe keys and segments do not collide with seeded corpus.
    ts = datetime(2099, 2, 1, tzinfo=timezone.utc)
    with connection() as conn:
        mid = _insert_dated_memory(
            conn,
            auth,
            content="moved to a new city after the breakup",
            when=ts,
        )
        _insert_dated_memory(
            conn,
            auth,
            content="first week in the new city, still unpacking",
            when=datetime(2099, 2, 10, tzinfo=timezone.utc),
        )
        conn.commit()

    report = run_chapter_proposals(auth, dry_run=False, use_llm=False, max_proposals=15)
    assert report.inserted >= 1
    hit = next(
        (p for p in report.proposals if str(mid) in p.get("evidence_memory_ids", [])),
        None,
    )
    assert hit is not None, "expected a proposal citing the inserted memory"
    chapter_id = hit["chapter_id"]

    with connection() as conn:
        row = fetch_one(
            conn,
            "SELECT status FROM life_chapters WHERE id = %s",
            (chapter_id,),
        )
        assert row["status"] == "proposed"
        ev = fetch_all(
            conn,
            "SELECT memory_id FROM life_chapter_evidence WHERE chapter_id = %s",
            (chapter_id,),
        )
        assert any(r["memory_id"] == mid for r in ev)


def test_migration_012_life_chapter_evidence_table(ensure_db):
    apply_migrations()
    with connection() as conn:
        row = fetch_one(
            conn,
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'life_chapter_evidence'
            """,
        )
        assert row is not None
