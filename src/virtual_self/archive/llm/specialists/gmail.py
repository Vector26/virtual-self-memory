from __future__ import annotations

from typing import Any

from virtual_self.archive.detect import DetectedFile
from virtual_self.archive.llm.specialists._core import (
    SpecialistAssignment,
    SpecialistResult,
    run_specialist,
)

SPECIALIST_ID = "gmail"


def _heuristic(file_sample: str, detected: dict[str, Any]) -> SpecialistResult:
    sample = file_sample[:2000]
    is_mbox = "From:" in sample and ("Subject:" in sample or detected.get("mime_hint") == "application/mbox")
    rel = str(detected.get("rel_path") or "").lower()
    path_ok = "mail/" in rel and rel.endswith(".mbox")
    should = bool(is_mbox or path_ok)
    hints = ["email_received", "email_sent"] if should else []
    return SpecialistResult(
        parse_strategy="mbox_stream" if should else "needs_mapping",
        event_type_hints=hints,
        pii_risk="high",
        should_parse=should,
        confidence=0.85 if should else 0.35,
        notes="mbox headers or Mail/*.mbox path; distinguish sent vs received via From",
    )


def classify(
    assignment: SpecialistAssignment | dict[str, Any],
    file_sample: str,
    detected: DetectedFile | dict[str, Any],
    *,
    use_llm: bool = True,
) -> SpecialistResult:
    return run_specialist(SPECIALIST_ID, assignment, file_sample, detected, _heuristic, use_llm=use_llm)
