from __future__ import annotations

import json
from typing import Any

from virtual_self.archive.detect import DetectedFile
from virtual_self.archive.llm.specialists._core import (
    SpecialistAssignment,
    SpecialistResult,
    run_specialist,
)

SPECIALIST_ID = "chrome"


def _heuristic(file_sample: str, detected: dict[str, Any]) -> SpecialistResult:
    rel = str(detected.get("rel_path") or "").lower()
    path_ok = "chrome/" in rel and "history.json" in rel
    nested = False
    try:
        data = json.loads(file_sample[:8000])
        if isinstance(data, dict) and "Browser History" in data or isinstance(data, list) and data and isinstance(data[0], dict) and "url" in data[0]:
            nested = True
    except json.JSONDecodeError:
        pass
    should = path_ok or nested
    return SpecialistResult(
        parse_strategy="json_nested" if should else "needs_mapping",
        event_type_hints=["browse"] if should else [],
        pii_risk="medium",
        should_parse=should,
        confidence=0.88 if should else 0.3,
        notes="Chrome history; passive browsing telemetry only",
    )


def classify(
    assignment: SpecialistAssignment | dict[str, Any],
    file_sample: str,
    detected: DetectedFile | dict[str, Any],
    *,
    use_llm: bool = True,
) -> SpecialistResult:
    return run_specialist(SPECIALIST_ID, assignment, file_sample, detected, _heuristic, use_llm=use_llm)
