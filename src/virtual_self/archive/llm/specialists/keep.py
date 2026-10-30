from __future__ import annotations

import json
from typing import Any

from virtual_self.archive.detect import DetectedFile
from virtual_self.archive.llm.specialists._core import (
    SpecialistAssignment,
    SpecialistResult,
    run_specialist,
)

SPECIALIST_ID = "keep"


def _heuristic(file_sample: str, detected: dict[str, Any]) -> SpecialistResult:
    rel = str(detected.get("rel_path") or "").lower()
    path_ok = rel.startswith("keep/") and rel.endswith(".json")
    note = False
    try:
        data = json.loads(file_sample[:8000])
        rows = data if isinstance(data, list) else [data]
        if rows and isinstance(rows[0], dict):
            row = rows[0]
            note = bool(row.get("textContent") or row.get("text") or row.get("title"))
    except json.JSONDecodeError:
        pass
    should = path_ok or note
    return SpecialistResult(
        parse_strategy="json_records" if should else "needs_mapping",
        event_type_hints=["note"] if should else [],
        pii_risk="high",
        should_parse=should,
        confidence=0.83 if should else 0.3,
        notes="Google Keep notes; user-authored text",
    )


def classify(
    assignment: SpecialistAssignment | dict[str, Any],
    file_sample: str,
    detected: DetectedFile | dict[str, Any],
    *,
    use_llm: bool = True,
) -> SpecialistResult:
    return run_specialist(SPECIALIST_ID, assignment, file_sample, detected, _heuristic, use_llm=use_llm)
