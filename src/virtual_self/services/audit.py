from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from virtual_self.persistence.db import execute


def log_event(
    conn,
    *,
    request_id: str | None,
    actor_id: UUID | None,
    operation: str,
    target_type: str | None = None,
    target_id: UUID | None = None,
    policy_decision: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    execute(
        conn,
        """
        INSERT INTO audit_events (request_id, actor_id, operation, target_type, target_id, policy_decision, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            request_id,
            actor_id,
            operation,
            target_type,
            target_id,
            policy_decision,
            json.dumps(metadata or {}),
        ),
    )
