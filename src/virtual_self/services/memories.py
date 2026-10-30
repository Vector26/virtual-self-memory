from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from virtual_self.auth.credentials import AuthContext
from virtual_self.domain.errors import (
    ForbiddenError,
    NotFoundError,
    PreconditionFailedError,
    ValidationError,
)
from virtual_self.domain.types import MemoryStatus, Trust
from virtual_self.persistence.db import execute, fetch_all, fetch_one, transaction
from virtual_self.policy.sensitivity import actor_may_read, actor_may_write, max_sensitivity
from virtual_self.services import audit, jobs, suppression
from virtual_self.services.indexing import index_memory_version
from virtual_self.services.retrieval import hybrid_search
from uuid6 import uuid7


def _version_row_to_api(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "version": row["version"],
        "content": row["content"],
        "status": row["status"],
        "trust": row["trust"],
        "sensitivity": row["sensitivity"],
        "confidence": row["confidence"],
        "valid_from": row["valid_from"].isoformat() if row.get("valid_from") else None,
        "valid_to": row["valid_to"].isoformat() if row.get("valid_to") else None,
        "created_at": row["created_at"].isoformat(),
    }


def memory_to_api(conn, memory_id: UUID) -> dict[str, Any]:
    mem = fetch_one(
        conn,
        "SELECT * FROM memories WHERE id = %s",
        (memory_id,),
    )
    if mem is None:
        raise NotFoundError("memory not found")
    ver = fetch_one(
        conn,
        "SELECT * FROM memory_versions WHERE id = %s",
        (mem["current_version_id"],),
    )
    if ver is None:
        raise NotFoundError("memory version not found")
    return {
        "id": str(mem["id"]),
        "subject_id": str(mem["subject_id"]),
        "kind": mem["kind"],
        "scope": mem["scope"],
        "current_version": _version_row_to_api(ver),
        "etag": f'"{mem["id"]}:{ver["version"]}"',
        "updated_at": mem["updated_at"].isoformat(),
    }


def create_memory(
    auth: AuthContext,
    *,
    kind: str,
    content: str,
    scope: str = "private",
    trust: str = Trust.USER_ASSERTED.value,
    sensitivity: int = 1,
    request_id: str | None = None,
) -> dict[str, Any]:
    if not actor_may_write(auth.sensitivity_max, sensitivity):
        raise ForbiddenError("sensitivity not allowed for actor")
    content = content.strip()
    if not content:
        raise ValidationError("content required")
    if len(content) > 32_000:
        raise ValidationError("content too large")

    with transaction() as conn:
        if suppression.is_suppressed(conn, auth.subject_id, kind, content):
            raise ValidationError("memory suppressed")

        memory_id = uuid7()
        version_id = uuid7()
        execute(
            conn,
            """
            INSERT INTO artifacts (id, artifact_type, subject_id, sensitivity)
            VALUES (%s, 'memory', %s, %s)
            """,
            (memory_id, auth.subject_id, sensitivity),
        )
        execute(
            conn,
            """
            INSERT INTO artifacts (id, artifact_type, subject_id, sensitivity)
            VALUES (%s, 'memory_version', %s, %s)
            """,
            (version_id, auth.subject_id, sensitivity),
        )
        execute(
            conn,
            """
            INSERT INTO memories (id, subject_id, kind, scope)
            VALUES (%s, %s, %s, %s)
            """,
            (memory_id, auth.subject_id, kind, scope),
        )
        execute(
            conn,
            """
            INSERT INTO memory_versions (
              id, memory_id, version, content, status, trust, sensitivity, created_by
            ) VALUES (%s, %s, 1, %s, %s, %s, %s, %s)
            """,
            (
                version_id,
                memory_id,
                content,
                MemoryStatus.ACTIVE.value,
                trust,
                sensitivity,
                auth.actor_id,
            ),
        )
        execute(
            conn,
            "UPDATE memories SET current_version_id = %s, updated_at = NOW() WHERE id = %s",
            (version_id, memory_id),
        )
        index_memory_version(conn, version_id, content)
        jobs.enqueue(
            conn,
            "memory.index",
            {"memory_version_id": str(version_id)},
            dedupe_key=f"idx:{version_id}",
        )
        audit.log_event(
            conn,
            request_id=request_id,
            actor_id=auth.actor_id,
            operation="memory.create",
            target_type="memory",
            target_id=memory_id,
        )
        return memory_to_api(conn, memory_id)


def get_memory(auth: AuthContext, memory_id: UUID) -> dict[str, Any]:
    with transaction() as conn:
        api = memory_to_api(conn, memory_id)
        sens = api["current_version"]["sensitivity"]
        if not actor_may_read(auth.sensitivity_max, sens):
            raise ForbiddenError("sensitivity not allowed for actor")
        if api["current_version"]["status"] == MemoryStatus.FORGOTTEN.value:
            raise NotFoundError("memory forgotten")
        execute(
            conn,
            "UPDATE memories SET retrieval_count = retrieval_count + 1, last_retrieved_at = NOW() WHERE id = %s",
            (memory_id,),
        )
        return api


def correct_memory(
    auth: AuthContext,
    memory_id: UUID,
    new_content: str,
    if_match: str | None,
    request_id: str | None = None,
) -> dict[str, Any]:
    new_content = new_content.strip()
    if not new_content:
        raise ValidationError("content required")
    with transaction() as conn:
        mem = fetch_one(conn, "SELECT * FROM memories WHERE id = %s", (memory_id,))
        if mem is None:
            raise NotFoundError("memory not found")
        cur = fetch_one(
            conn,
            "SELECT * FROM memory_versions WHERE id = %s",
            (mem["current_version_id"],),
        )
        if cur is None:
            raise NotFoundError("memory version not found")
        expected = f'"{memory_id}:{cur["version"]}"'
        if if_match and if_match != expected:
            raise PreconditionFailedError("version mismatch", details={"expected": expected})

        new_ver_id = uuid7()
        new_version = cur["version"] + 1
        sensitivity = max_sensitivity(cur["sensitivity"], 1)
        execute(
            conn,
            """
            UPDATE memory_versions SET status = %s, valid_to = NOW()
            WHERE id = %s
            """,
            (MemoryStatus.SUPERSEDED.value, cur["id"]),
        )
        execute(
            conn,
            """
            INSERT INTO artifacts (id, artifact_type, subject_id, sensitivity)
            VALUES (%s, 'memory_version', %s, %s)
            """,
            (new_ver_id, mem["subject_id"], sensitivity),
        )
        execute(
            conn,
            """
            INSERT INTO memory_versions (
              id, memory_id, version, content, status, trust, sensitivity,
              created_by, supersedes_version_id, change_reason
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                new_ver_id,
                memory_id,
                new_version,
                new_content,
                MemoryStatus.ACTIVE.value,
                Trust.USER_ASSERTED.value,
                sensitivity,
                auth.actor_id,
                cur["id"],
                "user_correction",
            ),
        )
        execute(
            conn,
            """
            INSERT INTO derivation_edges (parent_artifact_id, child_artifact_id, relationship)
            VALUES (%s, %s, 'supersedes')
            """,
            (cur["id"], new_ver_id),
        )
        execute(
            conn,
            "UPDATE memories SET current_version_id = %s, updated_at = NOW() WHERE id = %s",
            (new_ver_id, memory_id),
        )
        index_memory_version(conn, new_ver_id, new_content)
        jobs.enqueue(
            conn,
            "memory.index",
            {"memory_version_id": str(new_ver_id)},
            dedupe_key=f"idx:{new_ver_id}",
        )
        audit.log_event(
            conn,
            request_id=request_id,
            actor_id=auth.actor_id,
            operation="memory.correct",
            target_type="memory",
            target_id=memory_id,
        )
        return memory_to_api(conn, memory_id)


def forget_memory(auth: AuthContext, memory_id: UUID, request_id: str | None = None) -> dict[str, Any]:
    with transaction() as conn:
        mem = fetch_one(conn, "SELECT * FROM memories WHERE id = %s", (memory_id,))
        if mem is None:
            raise NotFoundError("memory not found")
        execute(
            conn,
            """
            UPDATE memory_versions SET status = %s
            WHERE id = %s
            """,
            (MemoryStatus.FORGOTTEN.value, mem["current_version_id"]),
        )
        audit.log_event(
            conn,
            request_id=request_id,
            actor_id=auth.actor_id,
            operation="memory.forget",
            target_type="memory",
            target_id=memory_id,
        )
        return memory_to_api(conn, memory_id)


def delete_memory(auth: AuthContext, memory_id: UUID, request_id: str | None = None) -> None:
    with transaction() as conn:
        mem = fetch_one(conn, "SELECT * FROM memories WHERE id = %s", (memory_id,))
        if mem is None:
            raise NotFoundError("memory not found")
        ver = fetch_one(
            conn,
            "SELECT content FROM memory_versions WHERE id = %s",
            (mem["current_version_id"],),
        )
        kind = mem["kind"]
        content = ver["content"] if ver else ""
        suppression.add_suppression(conn, auth.subject_id, kind, content, auth.actor_id)
        execute(
            conn,
            "UPDATE artifacts SET deleted_at = NOW(), state = 'deleted' WHERE id = %s",
            (memory_id,),
        )
        execute(
            conn,
            "UPDATE memory_versions SET status = %s WHERE memory_id = %s",
            (MemoryStatus.PENDING_DELETION.value, memory_id),
        )
        jobs.enqueue(
            conn,
            "memory.purge_projections",
            {"memory_id": str(memory_id)},
            dedupe_key=f"purge:{memory_id}",
        )
        audit.log_event(
            conn,
            request_id=request_id,
            actor_id=auth.actor_id,
            operation="memory.delete",
            target_type="memory",
            target_id=memory_id,
        )


def list_recent_chat_memories(auth: AuthContext, limit: int = 8) -> list[dict[str, Any]]:
    """Pinned user-asserted memories from CLI chat (Saved-Info style)."""
    limit = max(1, min(limit, 20))
    with transaction() as conn:
        rows = fetch_all(
            conn,
            """
            SELECT m.id AS memory_id, mv.content, mv.trust, mv.sensitivity, m.kind, m.updated_at
            FROM memories m
            JOIN memory_versions mv ON mv.id = m.current_version_id
            JOIN artifacts a ON a.id = m.id AND a.deleted_at IS NULL
            WHERE m.subject_id = %s
              AND m.scope = 'chat'
              AND mv.status = 'active'
              AND mv.trust = 'user_asserted'
              AND mv.sensitivity <= %s
            ORDER BY m.updated_at DESC
            LIMIT %s
            """,
            (auth.subject_id, auth.sensitivity_max, limit),
        )
    return [
        {
            "memory_id": str(r["memory_id"]),
            "content": r["content"],
            "trust": r["trust"],
            "sensitivity": r["sensitivity"],
            "kind": r["kind"],
            "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else None,
        }
        for r in rows
    ]


def search_memories(
    auth: AuthContext,
    query: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    q = query.strip()
    with transaction() as conn:
        return hybrid_search(
            conn,
            auth.subject_id,
            q,
            sensitivity_max=auth.sensitivity_max,
            limit=limit,
        )
