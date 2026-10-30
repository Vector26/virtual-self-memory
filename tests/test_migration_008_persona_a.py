from __future__ import annotations

import hashlib

from uuid import uuid4

from uuid6 import uuid7

from virtual_self.auth.credentials import resolve_auth
from virtual_self.config import get_settings
from virtual_self.persistence.db import connection, fetch_one
from virtual_self.persistence.migrate import apply_migrations
from virtual_self.services.chat_sessions import append_turn, open_cli_session


def test_migration_008_persona_a_idempotent_and_constraints(ensure_db):
    apply_migrations()
    apply_migrations()

    with connection() as conn:
        tables = {
            r["table_name"]
            for r in conn.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = current_schema()
                  AND table_name IN ('authored_texts', 'chat_turn_feedback')
                """
            ).fetchall()
        }
    assert tables == {"authored_texts", "chat_turn_feedback"}

    get_settings.cache_clear()
    token = get_settings().bootstrap_token
    auth = resolve_auth(f"Bearer {token.get_secret_value()}")

    body = f"migration 008-a smoke corpus line {uuid4()}"
    text_hash = hashlib.sha256(body.encode()).hexdigest()
    authored_id = uuid7()

    session_id = open_cli_session(auth)
    turn = append_turn(auth, session_id, role="user", content="feedback smoke turn")
    feedback_id = uuid7()

    with connection() as conn:
        conn.execute(
            """
            INSERT INTO authored_texts (
              id, subject_id, source_kind, source_ref, author, register,
              language, content, word_count, text_hash, sensitivity,
              authored_at, time_precision, time_source, split
            ) VALUES (
              %s, %s, 'manual', '{"smoke": true}', 'self', 'notes',
              'en', %s, 6, %s, 1,
              NOW(), 'day', 'explicit_in_text', 'train'
            )
            """,
            (authored_id, auth.subject_id, body, text_hash),
        )
        dup = conn.execute(
            """
            INSERT INTO authored_texts (
              id, subject_id, source_kind, author, register, content,
              word_count, text_hash
            ) VALUES (%s, %s, 'manual', 'self', 'notes', %s, 6, %s)
            ON CONFLICT (subject_id, text_hash) DO NOTHING
            """,
            (uuid7(), auth.subject_id, body, text_hash),
        )
        assert dup.rowcount == 0

        conn.execute(
            """
            INSERT INTO chat_turn_feedback (id, turn_id, rating, note, rewrite)
            VALUES (%s, %s, 'sounds_like_me', 'on voice', NULL)
            """,
            (feedback_id, turn["turn_id"]),
        )
        row = fetch_one(
            conn,
            "SELECT rating FROM chat_turn_feedback WHERE id = %s",
            (feedback_id,),
        )
        conn.commit()

    assert row is not None
    assert row["rating"] == "sounds_like_me"
