from __future__ import annotations

from typing import Any

from virtual_self.archive.detect import DetectedFile
from virtual_self.archive.llm.specialists._core import (
    SpecialistAssignment,
    SpecialistResult,
    run_specialist,
)

SPECIALIST_ID = "unknown"


def _heuristic(file_sample: str, detected: dict[str, Any]) -> SpecialistResult:
    product = str(detected.get("product") or "unknown")
    sample = file_sample.strip()
    empty = len(sample) < 8
    known = product != "unknown"
    should = not empty and known
    return SpecialistResult(
        parse_strategy="skip" if empty else ("needs_mapping" if not known else "native_parser"),
        event_type_hints=[],
        pii_risk="medium",
        should_parse=should,
        confidence=0.2 if not known else 0.45,
        notes="Unmapped export; defer parse until product identified",
    )


def classify(
    assignment: SpecialistAssignment | dict[str, Any],
    file_sample: str,
    detected: DetectedFile | dict[str, Any],
    *,
    use_llm: bool = True,
) -> SpecialistResult:
    return run_specialist(SPECIALIST_ID, assignment, file_sample, detected, _heuristic, use_llm=use_llm)
