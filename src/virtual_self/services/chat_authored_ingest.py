"""Ingest vself chat user turns → authored_texts (persona P1, rule 6)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from uuid6 import uuid7

from virtual_self.auth.credentials import AuthContext
from virtual_self.eval.heldout_snapshot import split_key_chat_turn, stable_split_bucket_from_key
from virtual_self.eval.notion_self_text import word_count
from virtual_self.persistence.db import connection, transaction

_MEMORY_SAVED_PREFIX = re.compile(r"^\[Memory saved:[^\]]*\]\s*", re.I)
_REGISTER = "casual_chat"


@dataclass
class ChatIngestStats:
    user_turns_total: int = 0
    turns_skipped_empty: int = 0
    turns_skipped_memory_only: int = 0
    rows_inserted: int = 0
    rows_skipped_duplicate: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_turns_total": self.user_turns_total,
            "turns_skipped_empty": self.turns_skipped_empty,
            "turns_skipped_memory_only": self.turns_skipped_memory_only,
            "rows_inserted": self.rows_inserted,
            "rows_skipped_duplicate": self.rows_skipped_duplicate,
            "turns_accounted": self.user_turns_total
            - self.turns_skipped_empty
            - self.turns_skipped_memory_only,
        }


def normalize_chat_user_content(content: str) -> str | None:
    """Strip system memory-save prefix; return None if nothing remains."""
    text = (content or "").strip()
    if not text:
        return None
    text = _MEMORY_SAVED_PREFIX.sub("", text).strip()
    if not text:
        return None
    return text


def _text_hash(turn_id: UUID, content: str) -> str:
    payload = f"chat_user_turn:{turn_id}\n{content}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _coerce_ts(raw: datetime | None) -> datetime | None:
    if raw is None:
        return None
    if raw.tzinfo is None:
        return raw.replace(tzinfo=timezone.utc)
    return raw


def run_chat_authored_ingest(
    auth: AuthContext,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    stats = ChatIngestStats()
    pending: list[tuple[Any, ...]] = []

    with connection() as conn:
        rows = conn.execute(
            """
            SELECT
              ct.id,
              ct.session_id,
              ct.turn_index,
              ct.content,
              ct.created_at
            FROM chat_turns ct
            JOIN chat_sessions cs ON cs.id = ct.session_id
            WHERE cs.subject_id = %s AND ct.role = 'user'
            ORDER BY ct.created_at, ct.turn_index
            """,
            (auth.subject_id,),
        ).fetchall()

    stats.user_turns_total = len(rows)

    for row in rows:
        turn_id: UUID = row["id"]
        raw_content = row["content"] or ""
        raw_stripped = raw_content.strip()
        if not raw_stripped:
            stats.turns_skipped_empty += 1
            continue

        content = normalize_chat_user_content(raw_content)
        if content is None:
            if _MEMORY_SAVED_PREFIX.match(raw_stripped):
                stats.turns_skipped_memory_only += 1
            else:
                stats.turns_skipped_empty += 1
            continue

        source_ref = {
            "turn_id": str(turn_id),
            "session_id": str(row["session_id"]),
            "turn_index": int(row["turn_index"]),
        }
        authored_at = _coerce_ts(row["created_at"])
        pending.append(
            (
                uuid7(),
                auth.subject_id,
                "chat_user_turn",
                json.dumps(source_ref),
                "self",
                1.0,
                _REGISTER,
                content,
                word_count(content),
                _text_hash(turn_id, content),
                1,
                authored_at,
                "instant",
                None,
                stable_split_bucket_from_key(split_key_chat_turn(turn_id)),
            )
        )

    inserted = 0
    dupes = 0
    if not dry_run and pending:
        with transaction() as conn:
            for row in pending:
                cur = conn.execute(
                    """
                    INSERT INTO authored_texts (
                      id, subject_id, source_kind, source_ref, author, author_confidence,
                      register, content, word_count, text_hash, sensitivity,
                      authored_at, time_precision, time_source, split
                    ) VALUES (
                      %s, %s, %s, %s::jsonb, %s, %s,
                      %s, %s, %s, %s, %s,
                      %s, %s, %s, %s
                    )
                    ON CONFLICT (subject_id, text_hash) DO NOTHING
                    """,
                    row,
                )
                if cur.rowcount:
                    inserted += 1
                else:
                    dupes += 1

    stats.rows_inserted = inserted
    stats.rows_skipped_duplicate = dupes

    out = stats.to_dict()
    out["dry_run"] = dry_run
    if not dry_run:
        with connection() as conn:
            n = conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM authored_texts
                WHERE subject_id = %s AND source_kind = 'chat_user_turn'
                """,
                (auth.subject_id,),
            ).fetchone()["n"]
        out["authored_chat_rows"] = int(n)
    return out


def sample_chat_authored_rows(subject_id: UUID, *, limit: int = 5) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT content, source_ref, register, authored_at
            FROM authored_texts
            WHERE subject_id = %s AND source_kind = 'chat_user_turn'
            ORDER BY authored_at NULLS LAST
            LIMIT %s
            """,
            (subject_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]
