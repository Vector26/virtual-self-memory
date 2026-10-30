"""T-6: temporal intent, validity-scoped retrieval, chapter bundle headers."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from uuid6 import uuid7

from virtual_self.auth.credentials import resolve_auth
from virtual_self.persistence.db import connection, execute, fetch_one, transaction
from virtual_self.services.chat_reply import _compose_turn_user
from virtual_self.services.memories import create_memory
from virtual_self.services.personality import bootstrap_personality, compile_context
from virtual_self.services.temporal_retrieval import (
    format_chapter_time_label,
    memory_valid_at,
    parse_temporal_intent,
)


def test_parse_temporal_intent_variants():
    assert parse_temporal_intent("what am I doing now?").kind == "now"
    assert parse_temporal_intent("back then I felt lost").kind == "then"
    assert parse_temporal_intent("in 2021 who was I").kind == "date"
    assert parse_temporal_intent("mid-2025 rebuild").as_of == datetime(2025, 7, 1, tzinfo=timezone.utc)
    assert parse_temporal_intent("things I always do").kind == "always"
    assert parse_temporal_intent("hello there").kind == "none"


def test_format_chapter_time_label_year_precision():
    ch = {
        "title": "post-breakup rebuild",
        "valid_from": datetime(2025, 3, 15, tzinfo=timezone.utc),
        "time_precision": "year",
    }
    label = format_chapter_time_label(ch)
    assert label.startswith("2025 · post-breakup rebuild")
    assert "year precision" in label
    assert "-03-15" not in label


def test_memory_valid_at_respects_valid_to():
    as_of = datetime(2024, 6, 1, tzinfo=timezone.utc)
    assert memory_valid_at(
        valid_from=datetime(2023, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2025, 1, 1, tzinfo=timezone.utc),
        invalidated_at=None,
        as_of=as_of,
    )
    assert not memory_valid_at(
        valid_from=datetime(2023, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2024, 1, 1, tzinfo=timezone.utc),
        invalidated_at=None,
        as_of=as_of,
    )


def _insert_active_chapter(conn, subject_id: UUID, title: str, *, vf, vt) -> UUID:
    career = fetch_one(conn, "SELECT id FROM life_strands WHERE slug = %s", ("career",))
    chapter_id = uuid7()
    execute(
        conn,
        """
        INSERT INTO life_chapters (
          id, subject_id, strand_id, title, summary, status,
          valid_from, valid_to, time_precision, valid_from_precision, valid_to_precision
        ) VALUES (%s, %s, %s, %s, %s, 'active', %s, %s, 'year', 'year', 'year')
        """,
        (chapter_id, subject_id, career["id"], title, "test chapter", vf, vt),
    )
    return chapter_id


def test_compile_context_now_prefers_open_validity_and_chapter_headers(ensure_db, auth_headers):
    auth = resolve_auth(auth_headers["Authorization"])
    bootstrap_personality(auth)
    needle = "T6_OPEN_VALIDITY_NEEDLE_XYZ"
    closed_needle = "T6_CLOSED_VALIDITY_NEEDLE_XYZ"

    now = datetime.now(timezone.utc)
    vf = datetime(2020, 1, 1, tzinfo=timezone.utc)

    chapter_title = "T6_UNIQUE_NEW_CITY_CHAPTER"
    with transaction() as conn:
        chapter_id = _insert_active_chapter(
            conn,
            auth.subject_id,
            chapter_title,
            vf=vf,
            vt=None,
        )
        open_mem = create_memory(
            auth,
            kind="fact",
            content=needle,
            scope="private",
        )
        closed_mem = create_memory(
            auth,
            kind="fact",
            content=closed_needle,
            scope="private",
        )
        for mid in (open_mem["id"], closed_mem["id"]):
            execute(
                conn,
                """
                UPDATE memories SET chapter_id = %s, valid_from = %s, time_precision = 'year'
                WHERE id = %s
                """,
                (chapter_id, vf, UUID(mid)),
            )
        execute(
            conn,
            "UPDATE memories SET valid_to = %s WHERE id = %s",
            (datetime(2022, 1, 1, tzinfo=timezone.utc), UUID(closed_mem["id"])),
        )

    bundle = compile_context(
        auth,
        query=f"what matters in {chapter_title} now? {needle[:12]}",
        memory_limit=8,
    )
    assert bundle["provenance"]["temporal_intent"] in ("now", "chapter")
    assert bundle.get("memory_chapters")
    headers = " ".join(g["header"] for g in bundle["memory_chapters"])
    assert chapter_title in headers
    flat = bundle["relevant_memories"]
    contents = " ".join(m.get("content") or "" for m in flat)
    assert needle in contents
    assert closed_needle not in contents

    user_msg = _compose_turn_user(bundle, "hello")
    assert " · " in user_msg
    assert needle in user_msg
    assert "2020 ·" in user_msg
