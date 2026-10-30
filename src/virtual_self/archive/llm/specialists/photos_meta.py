from __future__ import annotations

import json
from typing import Any

from virtual_self.archive.detect import DetectedFile
from virtual_self.archive.llm.specialists._core import (
    SpecialistAssignment,
    SpecialistResult,
    run_specialist,
)

SPECIALIST_ID = "photos_meta"


def _heuristic(file_sample: str, detected: dict[str, Any]) -> SpecialistResult:
    rel = str(detected.get("rel_path") or "").lower()
    path_ok = "google photos/" in rel and rel.endswith(".json")
    meta = False
    try:
        data = json.loads(file_sample[:8000])
        if isinstance(data, dict):
            meta = bool(
                data.get("photoTakenTime")
                or data.get("geoData")
                or data.get("title")
                or data.get("description")
            )
    except json.JSONDecodeError:
        pass
    should = path_ok or meta
    return SpecialistResult(
        parse_strategy="json_nested" if should else "needs_mapping",
        event_type_hints=["photo_metadata"] if should else [],
        pii_risk="medium",
        should_parse=should,
        confidence=0.8 if should else 0.3,
        notes="Photos sidecar JSON metadata only, not image bytes",
    )


def classify(
    assignment: SpecialistAssignment | dict[str, Any],
    file_sample: str,
    detected: DetectedFile | dict[str, Any],
    *,
    use_llm: bool = True,
) -> SpecialistResult:
    return run_specialist(SPECIALIST_ID, assignment, file_sample, detected, _heuristic, use_llm=use_llm)
