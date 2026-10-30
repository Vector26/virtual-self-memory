from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from uuid6 import uuid7

from virtual_self.auth.credentials import resolve_auth
from virtual_self.config import get_settings
from virtual_self.persistence.db import connection, execute
from virtual_self.persistence.migrate import apply_migrations
from virtual_self.persistence.db import transaction
from virtual_self.services.time_backfill import (
    _apply_resolution,
    _resolve_from_notion_meta,
    resolve_memory_time,
)


def test_import_cluster_stays_unknown():
    res = _resolve_from_notion_meta(
        {"created_time": "2024-06-01T12:00:00.000Z"},
        bulk_import_ts={"2024-06-01T12:00:00.000Z"},
    )
    assert res.time_source == "import"
    assert res.time_precision == "unknown"
    assert res.authored_at is None


def test_notion_created_day_precision():
    res = _resolve_from_notion_meta(
        {"created_time": "2024-01-15T08:00:00.000Z"},
        bulk_import_ts=set(),
    )
    assert res.time_source == "notion_created"
    assert res.time_precision == "day"
    assert res.authored_at is not None


def test_resolve_olap_product_time_source():
    olap = {
        "evt-1": {"event_id": "evt-1", "ts": "2023-05-01T10:00:00Z", "product": "google_photos"},
    }
    res = resolve_memory_time(
        scope="archive",
        source_type="archive_event",
        source_path=None,
        structured={"event_id": "evt-1"},
        event_id="evt-1",
        turn_created_at=None,
        notion_meta_by_rel={},
        bulk_import_ts=set(),
        olap=olap,
    )
    assert res.time_source == "exif"
    assert res.time_precision == "instant"


def test_pilot_apply_updates_memory(ensure_db):
    apply_migrations()
    get_settings.cache_clear()
    token = get_settings().bootstrap_token
    auth = resolve_auth(f"Bearer {token.get_secret_value()}")

    mem_id = uuid7()
    ver_id = uuid7()
    src_id = uuid7()
    ev_id = uuid7()
    with connection() as conn:
        execute(
            conn,
            """
            INSERT INTO sources (id, subject_id, source_type, external_id, sensitivity)
            VALUES (%s, %s, 'chat_cli', %s, 1)
            """,
            (src_id, auth.subject_id, f"chat-{uuid4()}"),
        )
        execute(
            conn,
            """
            INSERT INTO artifacts (id, artifact_type, subject_id, sensitivity)
            VALUES (%s, 'evidence', %s, 1)
            """,
            (ev_id, auth.subject_id),
        )
        ts = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        execute(
            conn,
            """
            INSERT INTO evidence_records (
              id, source_id, content_plain, structured_content, content_hash, sensitivity, classification_status
            ) VALUES (%s, %s, 'hello', '{}'::jsonb, %s, 1, 'classified')
            """,
            (ev_id, src_id, f"hash-{uuid4()}"),
        )
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
            INSERT INTO memories (id, subject_id, kind, scope, time_precision)
            VALUES (%s, %s, 'fact', 'chat', 'unknown')
            """,
            (mem_id, auth.subject_id),
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
            INSERT INTO memory_versions (id, memory_id, version, content, status, time_precision)
            VALUES (%s, %s, 1, 'fact body', 'active', 'unknown')
            """,
            (ver_id, mem_id),
        )
        execute(
            conn,
            "UPDATE memories SET current_version_id = %s WHERE id = %s",
            (ver_id, mem_id),
        )
        session_id = uuid7()
        execute(
            conn,
            """
            INSERT INTO chat_sessions (id, subject_id, actor_id, source_id)
            VALUES (%s, %s, %s, %s)
            """,
            (session_id, auth.subject_id, auth.actor_id, src_id),
        )
        execute(
            conn,
            """
            INSERT INTO chat_turns (
              id, session_id, turn_index, role, content, content_hash, evidence_record_id, created_at
            ) VALUES (%s, %s, 0, 'user', 'hello', %s, %s, %s)
            """,
            (uuid7(), session_id, f"t-{uuid4()}", ev_id, ts),
        )
        execute(
            conn,
            """
            INSERT INTO memory_evidence (
              memory_id, memory_version_id, evidence_record_id, relationship, extractor
            ) VALUES (%s, %s, %s, 'derived_from', 'test')
            """,
            (mem_id, ver_id, ev_id),
        )
        conn.commit()

    res = resolve_memory_time(
        scope="chat",
        source_type="chat_cli",
        source_path=None,
        structured={},
        event_id=None,
        turn_created_at=ts,
        notion_meta_by_rel={},
        bulk_import_ts=set(),
        olap={},
    )
    assert res.time_precision == "instant"
    with transaction() as conn:
        _apply_resolution(conn, mem_id, ver_id, res)

    with connection() as conn:
        row = conn.execute(
            "SELECT time_precision, authored_at FROM memories WHERE id = %s",
            (mem_id,),
        ).fetchone()
    assert row["time_precision"] == "instant"
    assert row["authored_at"] is not None
