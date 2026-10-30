from __future__ import annotations

from uuid import uuid4

from virtual_self.auth.credentials import resolve_auth
from virtual_self.config import get_settings
from virtual_self.persistence.db import connection
from virtual_self.persistence.migrate import apply_migrations
from virtual_self.services.chat_authored_ingest import (
    normalize_chat_user_content,
    run_chat_authored_ingest,
)
from virtual_self.services.chat_sessions import append_turn, open_cli_session


def test_normalize_chat_user_content_strips_memory_saved_prefix():
    assert normalize_chat_user_content("[Memory saved: fact]\nhello") == "hello"
    assert normalize_chat_user_content("[Memory saved: only this]") is None
    assert normalize_chat_user_content("  plain user text  ") == "plain user text"


def test_run_chat_authored_ingest_idempotent(ensure_db):
    apply_migrations()
    get_settings.cache_clear()
    token = get_settings().bootstrap_token
    auth = resolve_auth(f"Bearer {token.get_secret_value()}")

    session_id = open_cli_session(auth)
    append_turn(auth, session_id, role="user", content="casual corpus line one")
    append_turn(auth, session_id, role="user", content="casual corpus line two")
    append_turn(
        auth,
        session_id,
        role="user",
        content=f"[Memory saved: inline fact]\nuser after save {uuid4()}",
    )
    append_turn(auth, session_id, role="assistant", content="assistant must never ingest")
    append_turn(auth, session_id, role="user", content="[Memory saved: system only]")

    first = run_chat_authored_ingest(auth, dry_run=False)
    assert first["turns_skipped_memory_only"] >= 1

    again = run_chat_authored_ingest(auth, dry_run=False)
    assert again["rows_inserted"] == 0
    assert again["rows_skipped_duplicate"] == first["turns_accounted"]

    with connection() as conn:
        session_rows = conn.execute(
            """
            SELECT register, author, source_kind
            FROM authored_texts
            WHERE subject_id = %s
              AND source_kind = 'chat_user_turn'
              AND source_ref->>'session_id' = %s
            """,
            (auth.subject_id, str(session_id)),
        ).fetchall()
    assert len(session_rows) == 3

    with connection() as conn:
        rows = conn.execute(
            """
            SELECT register, author, source_kind
            FROM authored_texts
            WHERE subject_id = %s AND source_kind = 'chat_user_turn'
            """,
            (auth.subject_id,),
        ).fetchall()
    assert rows
    assert all(r["register"] == "casual_chat" for r in rows)
    assert all(r["author"] == "self" for r in rows)
    assert all(r["source_kind"] == "chat_user_turn" for r in rows)
