from __future__ import annotations

from collections.abc import Callable
from typing import Any

from virtual_self.archive.detect import DetectedFile
from virtual_self.archive.llm.specialists import (
    activity,
    chrome,
    gmail,
    keep,
    location,
    meta_messages,
    photos_meta,
    unknown,
    youtube,
)
from virtual_self.archive.llm.specialists._core import (
    MAX_SAMPLE_CHARS,
    SpecialistAssignment,
    SpecialistResult,
    build_user_payload,
    detected_to_dict,
    normalize_result,
    run_specialist,
)

# detect.py product keys → specialist_id (supervisor routing)
PRODUCT_TO_SPECIALIST_ID: dict[str, str] = {
    "mail": "gmail",
    "my_activity": "activity",
    "chrome": "chrome",
    "youtube": "youtube",
    "location": "location",
    "keep": "keep",
    "photos": "photos_meta",
    "messages": "meta_messages",
}

SPECIALIST_REGISTRY: dict[str, Callable[..., SpecialistResult]] = {
    "gmail": gmail.classify,
    "activity": activity.classify,
    "chrome": chrome.classify,
    "youtube": youtube.classify,
    "location": location.classify,
    "keep": keep.classify,
    "photos_meta": photos_meta.classify,
    "meta_messages": meta_messages.classify,
    "unknown": unknown.classify,
}


def classify_file(
    assignment: SpecialistAssignment | dict[str, Any],
    file_sample: str,
    detected: DetectedFile | dict[str, Any],
    *,
    use_llm: bool = True,
) -> SpecialistResult:
    sid = str(assignment.get("specialist_id") or "")
    fn = SPECIALIST_REGISTRY.get(sid) or SPECIALIST_REGISTRY["unknown"]
    return fn(assignment, file_sample, detected, use_llm=use_llm)


def resolve_specialist_id(detected: DetectedFile | dict[str, Any]) -> str:
    det = detected_to_dict(detected)
    product = str(det.get("product") or "unknown")
    if product in PRODUCT_TO_SPECIALIST_ID:
        return PRODUCT_TO_SPECIALIST_ID[product]
    return "unknown"


__all__ = [
    "MAX_SAMPLE_CHARS",
    "PRODUCT_TO_SPECIALIST_ID",
    "SPECIALIST_REGISTRY",
    "SpecialistAssignment",
    "SpecialistResult",
    "build_user_payload",
    "classify_file",
    "detected_to_dict",
    "normalize_result",
    "resolve_specialist_id",
    "run_specialist",
]
