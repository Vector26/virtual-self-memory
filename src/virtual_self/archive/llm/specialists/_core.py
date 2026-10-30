from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, TypedDict

from virtual_self.archive.detect import DetectedFile
from virtual_self.archive.llm.specialists.specialists_prompts import (
    PROMPT_VERSION,
    PROMPTS_BY_SPECIALIST_ID,
)
from virtual_self.config import get_settings
from virtual_self.providers.agent_chat import chat_json

MAX_SAMPLE_CHARS = 8000

REQUIRED_KEYS = frozenset(
    {"parse_strategy", "event_type_hints", "pii_risk", "should_parse", "confidence", "notes"}
)

VALID_PARSE_STRATEGIES = frozenset(
    {"native_parser", "mbox_stream", "json_records", "json_nested", "skip", "needs_mapping"}
)
VALID_PII = frozenset({"low", "medium", "high"})


class SpecialistResult(TypedDict):
    parse_strategy: str
    event_type_hints: list[str]
    pii_risk: str
    should_parse: bool
    confidence: float
    notes: str


class SpecialistAssignment(TypedDict, total=False):
    specialist_id: str
    reason: str
    supervisor_confidence: float


def detected_to_dict(detected: DetectedFile | dict[str, Any]) -> dict[str, Any]:
    if isinstance(detected, DetectedFile):
        return {
            "rel_path": detected.rel_path,
            "provider": detected.provider,
            "product": detected.product,
            "mime_hint": detected.mime_hint,
        }
    return dict(detected)


def normalize_result(raw: dict[str, Any] | None, *, fallback: SpecialistResult) -> SpecialistResult:
    if not raw or not isinstance(raw, dict):
        return fallback
    strategy = str(raw.get("parse_strategy") or fallback["parse_strategy"])
    if strategy not in VALID_PARSE_STRATEGIES:
        strategy = fallback["parse_strategy"]
    hints_raw = raw.get("event_type_hints")
    hints: list[str] = []
    if isinstance(hints_raw, list):
        for h in hints_raw:
            s = str(h).strip().lower()[:64]
            if s:
                hints.append(s)
    if not hints:
        hints = list(fallback["event_type_hints"])
    pii = str(raw.get("pii_risk") or fallback["pii_risk"]).lower()
    if pii not in VALID_PII:
        pii = fallback["pii_risk"]
    should = raw.get("should_parse")
    if isinstance(should, bool):
        should_parse = should
    else:
        should_parse = fallback["should_parse"]
    try:
        conf = float(raw.get("confidence", fallback["confidence"]))
    except (TypeError, ValueError):
        conf = fallback["confidence"]
    conf = max(0.0, min(1.0, conf))
    notes = str(raw.get("notes") or fallback["notes"])[:400]
    return SpecialistResult(
        parse_strategy=strategy,
        event_type_hints=hints,
        pii_risk=pii,
        should_parse=should_parse,
        confidence=conf,
        notes=notes,
    )


def build_user_payload(
    assignment: SpecialistAssignment | dict[str, Any],
    file_sample: str,
    detected: DetectedFile | dict[str, Any],
) -> str:
    payload = {
        "assignment": dict(assignment),
        "detected": detected_to_dict(detected),
        "file_sample": file_sample[:MAX_SAMPLE_CHARS],
        "prompt_version": PROMPT_VERSION,
    }
    return json.dumps(payload, ensure_ascii=False)


def run_specialist(
    specialist_id: str,
    assignment: SpecialistAssignment | dict[str, Any],
    file_sample: str,
    detected: DetectedFile | dict[str, Any],
    heuristic: Callable[[str, dict[str, Any]], SpecialistResult],
    *,
    use_llm: bool = True,
) -> SpecialistResult:
    det = detected_to_dict(detected)
    sample = file_sample[:MAX_SAMPLE_CHARS]
    fallback = heuristic(sample, det)
    system = PROMPTS_BY_SPECIALIST_ID.get(specialist_id)
    if not system:
        return fallback
    if use_llm and get_settings().archive_llm_enabled:
        user = build_user_payload(assignment, sample, det)
        raw = chat_json(system, user, temperature=0.0)
        return normalize_result(raw, fallback=fallback)
    return fallback
