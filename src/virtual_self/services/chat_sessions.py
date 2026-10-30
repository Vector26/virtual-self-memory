from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from virtual_self.auth.credentials import AuthContext
from virtual_self.persistence.db import execute, fetch_all, fetch_one, transaction
from uuid6 import uuid7

CHAT_SOURCE_TYPE = "chat_cli"


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def open_cli_session(auth: AuthContext) -> UUID:
    with transaction() as conn:
        session_id = uuid7()
        source_id = uuid7()
        execute(
            conn,
            """
            INSERT INTO sources (id, subject_id, source_type, external_id, source_path, content_hash, sensitivity)
            VALUES (%s, %s, %s, %s, %s, %s, 1)
            """,
            (
                source_id,
                auth.subject_id,
                CHAT_SOURCE_TYPE,
                str(session_id),
                f"chat_cli:{session_id}",
                "",
            ),
        )
        execute(
            conn,
            """
            INSERT INTO chat_sessions (id, subject_id, actor_id, channel, source_id, metadata)
            VALUES (%s, %s, %s, 'cli', %s, '{}'::jsonb)
            """,
            (session_id, auth.subject_id, auth.actor_id, source_id),
        )
        return session_id


def append_turn(
    auth: AuthContext,
    session_id: UUID,
    *,
    role: str,
    content: str,
) -> dict[str, Any]:
    content = content.strip()
    if not content:
        raise ValueError("empty turn content")
    ch = _content_hash(content)
    with transaction() as conn:
        sess = fetch_one(
            conn,
            "SELECT id, source_id, subject_id FROM chat_sessions WHERE id = %s AND subject_id = %s",
            (session_id, auth.subject_id),
        )
        if sess is None:
            raise ValueError("chat session not found")
        row = fetch_one(
            conn,
            "SELECT COALESCE(MAX(turn_index), -1) + 1 AS n FROM chat_turns WHERE session_id = %s",
            (session_id,),
        )
        turn_index = int(row["n"])
        turn_id = uuid7()
        evidence_id: UUID | None = None
        if role in ("user", "assistant"):
            evidence_id = uuid7()
            execute(
                conn,
                """
                INSERT INTO artifacts (id, artifact_type, subject_id, sensitivity)
                VALUES (%s, 'evidence', %s, 1)
                """,
                (evidence_id, auth.subject_id),
            )
            structured = {"role": role, "session_id": str(session_id), "turn_index": turn_index}
            execute(
                conn,
                """
                INSERT INTO evidence_records (
                  id, source_id, content_plain, structured_content,
                  content_hash, sensitivity, classification_status, speaker
                ) VALUES (%s, %s, %s, %s::jsonb, %s, 1, 'classified', %s)
                """,
                (
                    evidence_id,
                    sess["source_id"],
                    content,
                    json.dumps(structured),
                    ch,
                    role,
                ),
            )
        execute(
            conn,
            """
            INSERT INTO chat_turns (
              id, session_id, turn_index, role, content, content_hash, evidence_record_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (turn_id, session_id, turn_index, role, content, ch, evidence_id),
        )
        execute(
            conn,
            "UPDATE chat_sessions SET last_activity_at = NOW() WHERE id = %s",
            (session_id,),
        )
        return {
            "turn_id": turn_id,
            "turn_index": turn_index,
            "evidence_record_id": evidence_id,
        }


def load_turn_window(session_id: UUID, turn_id: UUID, *, max_turns: int) -> list[dict[str, Any]]:
    with transaction() as conn:
        anchor = fetch_one(
            conn,
            "SELECT turn_index FROM chat_turns WHERE id = %s AND session_id = %s",
            (turn_id, session_id),
        )
        if anchor is None:
            return []
        min_index = max(0, int(anchor["turn_index"]) - max_turns + 1)
        rows = fetch_all(
            conn,
            """
            SELECT role, content, turn_index
            FROM chat_turns
            WHERE session_id = %s AND turn_index >= %s AND turn_index <= %s
            ORDER BY turn_index
            """,
            (session_id, min_index, anchor["turn_index"]),
        )
        return [{"role": r["role"], "content": r["content"]} for r in rows]


def get_turn(session_id: UUID, turn_id: UUID) -> dict[str, Any] | None:
    with transaction() as conn:
        return fetch_one(
            conn,
            """
            SELECT id, session_id, role, content, evidence_record_id, turn_index
            FROM chat_turns WHERE id = %s AND session_id = %s
            """,
            (turn_id, session_id),
        )
