from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from virtual_self.config import get_settings
from virtual_self.persistence.db import execute, fetch_all, fetch_one, transaction
from uuid6 import uuid7


def enqueue(
    conn,
    job_type: str,
    payload: dict[str, Any],
    dedupe_key: str | None = None,
) -> UUID:
    if dedupe_key:
        existing = fetch_one(
            conn,
            """
            SELECT id FROM jobs
            WHERE job_type = %s AND dedupe_key = %s AND state IN ('pending', 'running')
            LIMIT 1
            """,
            (job_type, dedupe_key),
        )
        if existing:
            return existing["id"]
    jid = uuid7()
    execute(
        conn,
        """
        INSERT INTO jobs (id, job_type, dedupe_key, payload_ref)
        VALUES (%s, %s, %s, %s::jsonb)
        """,
        (jid, job_type, dedupe_key, json.dumps(payload)),
    )
    return jid


def claim_next(owner: str, job_type: str | None = None) -> dict[str, Any] | None:
    lease_sec = get_settings().job_lease_seconds
    with transaction() as conn:
        if job_type:
            row = fetch_one(
                conn,
                """
                SELECT id FROM jobs
                WHERE state = 'pending' AND available_at <= NOW() AND job_type = %s
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                (job_type,),
            )
        else:
            row = fetch_one(
                conn,
                """
                SELECT id FROM jobs
                WHERE state = 'pending' AND available_at <= NOW()
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
            )
        if row is None:
            return None
        jid = row["id"]
        execute(
            conn,
            """
            UPDATE jobs SET state = 'running', lease_owner = %s,
              lease_expires = NOW() + (%s || ' seconds')::interval,
              attempts = attempts + 1, updated_at = NOW()
            WHERE id = %s
            """,
            (owner, str(lease_sec), jid),
        )
        return fetch_one(conn, "SELECT * FROM jobs WHERE id = %s", (jid,))


def complete_job(job_id: UUID, success: bool, error: str | None = None) -> None:
    with transaction() as conn:
        row = fetch_one(conn, "SELECT attempts, max_attempts FROM jobs WHERE id = %s", (job_id,))
        if row is None:
            return
        if success:
            execute(
                conn,
                "UPDATE jobs SET state = 'completed', last_error = NULL, updated_at = NOW() WHERE id = %s",
                (job_id,),
            )
            return
        dead = row["attempts"] >= row["max_attempts"]
        new_state = "dead" if dead else "pending"
        delay = min(300, 2 ** row["attempts"])
        execute(
            conn,
            """
            UPDATE jobs SET state = %s, last_error = %s,
              available_at = NOW() + (%s || ' seconds')::interval,
              lease_owner = NULL, lease_expires = NULL, updated_at = NOW()
            WHERE id = %s
            """,
            (new_state, error, str(delay), job_id),
        )


def release_stale_leases() -> int:
    with transaction() as conn:
        return execute(
            conn,
            """
            UPDATE jobs SET state = 'pending', lease_owner = NULL, lease_expires = NULL
            WHERE state = 'running' AND lease_expires < NOW()
            """,
        )
