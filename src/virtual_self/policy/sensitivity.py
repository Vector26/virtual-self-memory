from __future__ import annotations

import re

# Path fragments from architecture plan — conservative defaults
_BLOCKED_PATH_RE = re.compile(
    r"(?i)(finance|mental.?health|triggers|heal_?repair|people/.*/score)",
)
_JOURNAL_BODY_RE = re.compile(r"(?i)/journal/")

SENSITIVITY_PUBLIC = 0
SENSITIVITY_PERSONAL = 1
SENSITIVITY_PRIVATE = 2
SENSITIVITY_HIGHLY_PRIVATE = 3
SENSITIVITY_RESTRICTED = 4


def classify_source_path(path: str | None, declared: int | None = None) -> int:
    if declared is not None:
        return max(0, min(4, declared))
    if not path:
        return SENSITIVITY_PERSONAL
    if _BLOCKED_PATH_RE.search(path):
        return SENSITIVITY_HIGHLY_PRIVATE
    if _JOURNAL_BODY_RE.search(path):
        return SENSITIVITY_PRIVATE
    return SENSITIVITY_PERSONAL


def max_sensitivity(*levels: int) -> int:
    return max(levels) if levels else SENSITIVITY_PERSONAL


def actor_may_read(actor_max: int, record_level: int) -> bool:
    return record_level <= actor_max


def actor_may_write(actor_max: int, record_level: int) -> bool:
    return record_level <= actor_max and record_level < SENSITIVITY_RESTRICTED
