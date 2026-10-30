from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from virtual_self.auth.credentials import AuthContext
from virtual_self.config import get_settings
from virtual_self.domain.types import Trust
from virtual_self.enrichment.chat_memory_prompts import EXTRACTOR_VERSION
from virtual_self.observability.logging import get_logger
from virtual_self.observability.notify import NotifyCategory, notify_user
from virtual_self.persistence.db import execute, fetch_one, transaction
from virtual_self.services import jobs
from virtual_self.services.chat_memory_extract import extract_chat_memories, parse_candidates
from virtual_self.services.chat_sessions import get_turn, load_turn_window
from virtual_self.services.memories import create_memory, search_memories
from uuid6 import uuid7

log = get_logger(__name__)
EXTRACTOR_NAME = "chat_memory_classifier"


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _is_duplicate(auth: AuthContext, content: str) -> bool:
    norm = _normalize(content)
    hits = search_memories(auth, content, limit=5)
    for h in hits:
        if _normalize(h.get("content") or "") == norm:
            return True
        if norm in _normalize(h.get("content") or ""):
            return True
    return False


def _record_run(
    conn,
    *,
    session_id: UUID,
    turn_id: UUID,
    raw: dict[str, Any] | None,
    decision: str,
    skip_reason: str | None = None,
    memory_id: UUID | None = None,
) -> None:
    run_id = uuid7()
    execute(
        conn,
        """
        INSERT INTO memory_extraction_runs (
          id, session_id, turn_id, extractor, prompt_version, raw_json, decision, skip_reason, memory_id
        ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
        """,
        (
            run_id,
            session_id,
            turn_id,
            EXTRACTOR_NAME,
            EXTRACTOR_VERSION,
            json.dumps(raw) if raw is not None else None,
            decision,
            skip_reason,
            memory_id,
        ),
    )


def _link_evidence(conn, memory_id: UUID, version_id: UUID, evidence_id: UUID) -> None:
    execute(
        conn,
        """
        INSERT INTO memory_evidence (
          memory_id, memory_version_id, evidence_record_id, relationship, extractor, extractor_version
        ) VALUES (%s, %s, %s, 'derived_from', %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (memory_id, version_id, evidence_id, EXTRACTOR_NAME, EXTRACTOR_VERSION),
    )


def promote_candidate(
    auth: AuthContext,
    candidate: dict[str, Any],
    *,
    evidence_record_id: UUID | None,
) -> dict[str, Any] | None:
    settings = get_settings()
    if candidate.get("confidence", 0) < settings.chat_memory_min_confidence:
        return None
    content = candidate["content"]
    if _is_duplicate(auth, content):
        log.info("chat_memory_duplicate_skip", content=content[:80])
        return None
    kind = candidate.get("kind") or "fact"
    api = create_memory(
        auth,
        kind=kind,
        content=content,
        scope="chat",
        trust=Trust.USER_ASSERTED.value,
    )
    if evidence_record_id:
        with transaction() as conn:
            mem_id = UUID(api["id"])
            ver_row = fetch_one(
                conn,
                "SELECT current_version_id FROM memories WHERE id = %s",
                (mem_id,),
            )
            if ver_row and ver_row["current_version_id"]:
                _link_evidence(conn, mem_id, ver_row["current_version_id"], evidence_record_id)
    notify_user(
        f"Virtual Self saved a memory from chat: {content[:200]}",
        category=NotifyCategory.CHAT_MEMORY,
    )
    log.info("chat_memory_saved", memory_id=api["id"], content=content[:80])
    return api


def run_chat_memory_extract(
    auth: AuthContext,
    session_id: UUID,
    turn_id: UUID,
) -> dict[str, Any]:
    settings = get_settings()
    if not settings.chat_memory_enabled:
        return {"decision": "skipped", "reason": "disabled"}

    with transaction() as conn:
        sess = fetch_one(
            conn,
            "SELECT subject_id FROM chat_sessions WHERE id = %s",
            (session_id,),
        )
    if sess is None:
        return {"decision": "failed", "reason": "session_not_found"}
    if sess["subject_id"] != auth.subject_id:
        return {"decision": "failed", "reason": "session_subject_mismatch"}

    turn = get_turn(session_id, turn_id)
    if turn is None:
        return {"decision": "failed", "reason": "turn_not_found"}
    if turn["role"] != "user":
        return {"decision": "skipped", "reason": "not_user_turn"}

    window = load_turn_window(session_id, turn_id, max_turns=settings.chat_memory_turn_window * 2)
    user_message = turn["content"]
    assistant_message = ""
    with transaction() as conn:
        asst_turn = fetch_one(
            conn,
            """
            SELECT content, evidence_record_id FROM chat_turns
            WHERE session_id = %s AND role = 'assistant' AND turn_index = %s
            """,
            (session_id, int(turn["turn_index"]) + 1),
        )
    if asst_turn:
        assistant_message = asst_turn["content"] or ""

    evidence_id = turn.get("evidence_record_id")

    try:
        candidates, raw = extract_chat_memories(
            window,
            user_message=user_message,
            assistant_message=assistant_message,
        )
    except Exception as exc:
        log.exception("chat_memory_extract_failed", error=str(exc))
        with transaction() as conn:
            _record_run(
                conn,
                session_id=session_id,
                turn_id=turn_id,
                raw=None,
                decision="failed",
                skip_reason=str(exc)[:500],
            )
        return {"decision": "failed", "reason": str(exc)}

    parsed = parse_candidates(raw)
    assistant_derived = any(c.get("source_speaker") == "assistant" for c in parsed)

    saved: list[str] = []
    for c in candidates:
        api = promote_candidate(auth, c, evidence_record_id=evidence_id)
        if api:
            saved.append(api["id"])

    with transaction() as conn:
        if saved:
            _record_run(
                conn,
                session_id=session_id,
                turn_id=turn_id,
                raw=raw,
                decision="saved",
                memory_id=UUID(saved[0]),
            )
        elif assistant_derived and not candidates:
            _record_run(
                conn,
                session_id=session_id,
                turn_id=turn_id,
                raw=raw,
                decision="excluded",
                skip_reason="assistant_derived_candidates",
            )
        elif not candidates:
            _record_run(
                conn,
                session_id=session_id,
                turn_id=turn_id,
                raw=raw,
                decision="skipped",
                skip_reason="no_candidates",
            )
        else:
            _record_run(
                conn,
                session_id=session_id,
                turn_id=turn_id,
                raw=raw,
                decision="skipped",
                skip_reason="gates_or_duplicate",
            )

    if assistant_derived and not candidates and not saved:
        log.info(
            "chat_memory_excluded",
            reason="assistant_derived_candidates",
            turn_id=str(turn_id),
        )
        return {"decision": "excluded", "reason": "assistant_derived_candidates"}
    if not candidates:
        log.info("chat_memory_skipped", reason="no_candidates", turn_id=str(turn_id))
        return {"decision": "skipped", "reason": "no_candidates"}
    if not saved:
        log.info("chat_memory_skipped", reason="gates_or_duplicate", turn_id=str(turn_id))
        return {"decision": "skipped", "reason": "gates_or_duplicate"}
    return {"decision": "saved", "memory_ids": saved}


def enqueue_chat_memory_extract(auth: AuthContext, session_id: UUID, turn_id: UUID) -> None:
    settings = get_settings()
    if not settings.chat_memory_enabled:
        return
    with transaction() as conn:
        jobs.enqueue(
            conn,
            "chat.memory.extract",
            {
                "subject_id": str(auth.subject_id),
                "actor_id": str(auth.actor_id),
                "session_id": str(session_id),
                "turn_id": str(turn_id),
            },
            dedupe_key=f"chat-extract:{turn_id}",
        )
