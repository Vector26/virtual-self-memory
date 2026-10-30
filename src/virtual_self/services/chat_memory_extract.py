from __future__ import annotations

from typing import Any

from virtual_self.enrichment.chat_memory_prompts import EXTRACTOR_VERSION, SYSTEM, USER_TEMPLATE
from virtual_self.enrichment.grounding import adjust_confidence, grounding_score, passes_grounding
from virtual_self.providers.agent_chat import classifier_chat_json
from virtual_self.observability.logging import get_logger

log = get_logger(__name__)


def _format_window(turns: list[dict[str, Any]]) -> str:
    lines = []
    for t in turns:
        role = t.get("role", "user").upper()
        lines.append(f"{role}: {t.get('content', '')}")
    return "\n".join(lines) if lines else "(empty)"


def parse_candidates(raw: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not raw or not isinstance(raw, dict):
        return []
    items = raw.get("candidates")
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        out.append(
            {
                "content": content,
                "kind": str(item.get("kind") or "fact").strip().lower()[:64],
                "source_speaker": str(item.get("source_speaker") or "").strip().lower(),
                "should_persist": bool(item.get("should_persist")),
                "session_only": bool(item.get("session_only")),
                "confidence": float(item.get("confidence") or 0.0),
            }
        )
    return out


def extract_chat_memories(
    turns: list[dict[str, Any]],
    *,
    user_message: str,
    assistant_message: str,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """LLM classifier; returns validated candidates and raw JSON."""
    user_msg = user_message.strip()
    user_blob = "\n".join(
        t["content"] for t in turns if t.get("role") == "user"
    ) or user_msg
    prompt_user = USER_TEMPLATE.format(
        window=_format_window(turns),
        user_message=user_msg,
        assistant_message=assistant_message.strip()[:4000],
    )
    raw = classifier_chat_json(SYSTEM, prompt_user, temperature=0.1)
    candidates = parse_candidates(raw)
    validated: list[dict[str, Any]] = []
    for c in candidates:
        if c["source_speaker"] != "user":
            continue
        if not c["should_persist"] or c["session_only"]:
            continue
        score = grounding_score(c["content"], user_blob, None)
        if not passes_grounding(c["content"], user_blob, None, min_score=0.25):
            log.info("chat_memory_grounding_fail", content=c["content"][:80], score=score)
            continue
        conf = adjust_confidence(c["content"], c["confidence"], score)
        c["confidence"] = conf
        validated.append(c)
    return validated, raw
