from __future__ import annotations

import json
from typing import Any

from virtual_self.archive.detect import DetectedFile
from virtual_self.archive.llm.specialists._core import (
    SpecialistAssignment,
    SpecialistResult,
    run_specialist,
)

SPECIALIST_ID = "activity"


def _heuristic(file_sample: str, detected: dict[str, Any]) -> SpecialistResult:
    rel = str(detected.get("rel_path") or "")
    path_ok = "my activity" in rel.lower() and rel.lower().endswith("myactivity.json")
    parsed = False
    has_rows = False
    try:
        data = json.loads(file_sample[:8000])
        if isinstance(data, list) and data and isinstance(data[0], dict):
            parsed = True
            has_rows = bool(data[0].get("title") or data[0].get("titleUrl") or data[0].get("time"))
    except json.JSONDecodeError:
        pass
    should = path_ok or (parsed and has_rows)
    return SpecialistResult(
        parse_strategy="json_records" if should else "needs_mapping",
        event_type_hints=["activity"] if should else [],
        pii_risk="medium",
        should_parse=should,
        confidence=0.8 if should else 0.3,
        notes="My Activity JSON; passive product telemetry unless create/post actions",
    )


def classify(
    assignment: SpecialistAssignment | dict[str, Any],
    file_sample: str,
    detected: DetectedFile | dict[str, Any],
    *,
    use_llm: bool = True,
) -> SpecialistResult:
    return run_specialist(SPECIALIST_ID, assignment, file_sample, detected, _heuristic, use_llm=use_llm)
