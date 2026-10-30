from __future__ import annotations

import hashlib
import hmac
from uuid import UUID

from virtual_self.config import get_settings
from virtual_self.persistence.db import execute, fetch_one, transaction
from uuid6 import uuid7


def _hmac_key() -> bytes:
    settings = get_settings()
    secret = (
        settings.field_encryption_key.get_secret_value()
        if settings.field_encryption_key
        else (settings.bootstrap_token.get_secret_value() if settings.bootstrap_token else "dev-insecure")
    )
    return hashlib.sha256(secret.encode()).digest()


def fingerprint(subject_id: UUID, kind: str, content: str) -> bytes:
    msg = f"{subject_id}:{kind}:{content.strip().lower()}".encode()
    return hmac.new(_hmac_key(), msg, hashlib.sha256).digest()


def is_suppressed(conn, subject_id: UUID, kind: str, content: str) -> bool:
    fp = fingerprint(subject_id, kind, content)
    row = fetch_one(
        conn,
        """
        SELECT 1 FROM memory_suppressions
        WHERE subject_id = %s AND fingerprint_hmac = %s
          AND (expires_at IS NULL OR expires_at > NOW())
        """,
        (subject_id, fp),
    )
    return row is not None


def add_suppression(
    conn,
    subject_id: UUID,
    kind: str,
    content: str,
    actor_id: UUID,
    reason: str = "hard_delete",
) -> UUID:
    sid = uuid7()
    fp = fingerprint(subject_id, kind, content)
    execute(
        conn,
        """
        INSERT INTO memory_suppressions (id, subject_id, fingerprint_hmac, reason, requested_by)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (subject_id, fingerprint_hmac) DO NOTHING
        """,
        (sid, subject_id, fp, reason, actor_id),
    )
    return sid
