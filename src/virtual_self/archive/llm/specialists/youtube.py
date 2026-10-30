from __future__ import annotations

import json
from typing import Any

from virtual_self.archive.detect import DetectedFile
from virtual_self.archive.llm.specialists._core import (
    SpecialistAssignment,
    SpecialistResult,
    run_specialist,
)

SPECIALIST_ID = "youtube"


def _heuristic(file_sample: str, detected: dict[str, Any]) -> SpecialistResult:
    rel = str(detected.get("rel_path") or "").lower()
    path_ok = "youtube" in rel and "/history/" in rel and rel.endswith(".json")
    watch_rows = False
    try:
        data = json.loads(file_sample[:8000])
        rows = data if isinstance(data, list) else (data.get("Array") if isinstance(data, dict) else [])
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            watch_rows = bool(rows[0].get("title") or rows[0].get("titleUrl") or rows[0].get("time"))
    except json.JSONDecodeError:
        pass
    should = path_ok or watch_rows
    return SpecialistResult(
        parse_strategy="json_records" if should else "needs_mapping",
        event_type_hints=["watch"] if should else [],
        pii_risk="low",
        should_parse=should,
        confidence=0.82 if should else 0.3,
        notes="YouTube watch history; passive viewing telemetry",
    )


def classify(
    assignment: SpecialistAssignment | dict[str, Any],
    file_sample: str,
    detected: DetectedFile | dict[str, Any],
    *,
    use_llm: bool = True,
) -> SpecialistResult:
    return run_specialist(SPECIALIST_ID, assignment, file_sample, detected, _heuristic, use_llm=use_llm)
