from __future__ import annotations

import json
from typing import Any

from virtual_self.archive.detect import DetectedFile
from virtual_self.archive.llm.specialists._core import (
    SpecialistAssignment,
    SpecialistResult,
    run_specialist,
)

SPECIALIST_ID = "location"


def _heuristic(file_sample: str, detected: dict[str, Any]) -> SpecialistResult:
    rel = str(detected.get("rel_path") or "").lower()
    path_ok = "location history" in rel
    geo = False
    if "latitude" in file_sample.lower() or "longitude" in file_sample.lower():
        geo = True
    if not geo:
        try:
            data = json.loads(file_sample[:8000])
            if isinstance(data, dict):
                geo = bool(data.get("locations") or data.get("timelineObjects"))
        except json.JSONDecodeError:
            pass
    should = path_ok or geo
    return SpecialistResult(
        parse_strategy="native_parser" if should else "needs_mapping",
        event_type_hints=["location"] if should else [],
        pii_risk="high",
        should_parse=should,
        confidence=0.84 if should else 0.25,
        notes="Location History; passive precise geo telemetry",
    )


def classify(
    assignment: SpecialistAssignment | dict[str, Any],
    file_sample: str,
    detected: DetectedFile | dict[str, Any],
    *,
    use_llm: bool = True,
) -> SpecialistResult:
    return run_specialist(SPECIALIST_ID, assignment, file_sample, detected, _heuristic, use_llm=use_llm)
