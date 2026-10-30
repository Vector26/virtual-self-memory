from __future__ import annotations

import json
from typing import Any

from virtual_self.archive.detect import DetectedFile
from virtual_self.archive.llm.specialists._core import (
    SpecialistAssignment,
    SpecialistResult,
    run_specialist,
)

SPECIALIST_ID = "meta_messages"


def _heuristic(file_sample: str, detected: dict[str, Any]) -> SpecialistResult:
    rel = str(detected.get("rel_path") or "").lower()
    path_ok = "messages/inbox/" in rel and rel.endswith(".json")
    msgs = False
    try:
        data = json.loads(file_sample[:8000])
        if isinstance(data, dict) and isinstance(data.get("messages"), list):
            msgs = bool(data["messages"])
    except json.JSONDecodeError:
        pass
    should = path_ok or msgs
    return SpecialistResult(
        parse_strategy="json_nested" if should else "needs_mapping",
        event_type_hints=["message"] if should else [],
        pii_risk="high",
        should_parse=should,
        confidence=0.86 if should else 0.3,
        notes="Meta inbox export; separate self-authored vs other senders",
    )


def classify(
    assignment: SpecialistAssignment | dict[str, Any],
    file_sample: str,
    detected: DetectedFile | dict[str, Any],
    *,
    use_llm: bool = True,
) -> SpecialistResult:
    return run_specialist(SPECIALIST_ID, assignment, file_sample, detected, _heuristic, use_llm=use_llm)
