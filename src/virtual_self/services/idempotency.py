from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from virtual_self.config import get_settings
from virtual_self.persistence.db import execute, fetch_one, transaction


def _hash_body(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def get_cached(key: str, actor_id: UUID, body: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
    req_hash = _hash_body(body)
    with transaction() as conn:
        row = fetch_one(
            conn,
            """
            SELECT status_code, response_body, request_hash, expires_at
            FROM idempotency_keys
            WHERE key = %s AND actor_id = %s
            """,
            (key, actor_id),
        )
        if row is None:
            return None
        if row["expires_at"] < datetime.now(timezone.utc):
            execute(conn, "DELETE FROM idempotency_keys WHERE key = %s", (key,))
            return None
        if row["request_hash"] != req_hash:
            from virtual_self.domain.errors import ConflictError

            raise ConflictError("idempotency key reused with different payload")
        return row["status_code"], row["response_body"]


def store_cached(
    key: str,
    actor_id: UUID,
    body: dict[str, Any],
    status_code: int,
    response: dict[str, Any],
) -> None:
    ttl = get_settings().idempotency_ttl_hours
    req_hash = _hash_body(body)
    expires = datetime.now(timezone.utc) + timedelta(hours=ttl)
    with transaction() as conn:
        execute(
            conn,
            """
            INSERT INTO idempotency_keys (key, actor_id, request_hash, response_body, status_code, expires_at)
            VALUES (%s, %s, %s, %s::jsonb, %s, %s)
            ON CONFLICT (key) DO UPDATE SET
              response_body = EXCLUDED.response_body,
              status_code = EXCLUDED.status_code,
              expires_at = EXCLUDED.expires_at
            """,
            (key, actor_id, req_hash, json.dumps(response), status_code, expires),
        )
