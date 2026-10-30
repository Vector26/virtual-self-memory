"""Deterministic temporal QA metrics (§15.9 false-precision + bundle validity)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

DatePrecision = Literal["year", "month", "day", "unknown"]

_ISO_DATE = re.compile(r"\b(19|20)\d{2}-\d{2}-\d{2}\b")
_ISO_MONTH = re.compile(r"\b(19|20)\d{2}-(?:0[1-9]|1[0-2])\b")
_YEAR_ONLY = re.compile(r"\b(19|20)\d{2}\b")
_MONTH_NAME_DAY = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+(19|20)\d{2}\b",
    re.I,
)
_SLASH_DATE = re.compile(r"\b(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])/(19|20)\d{2}\b")


@dataclass
class TemporalCheckResult:
    passed: bool
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "flags": list(self.flags)}


def check_reply_date_precision(
    reply: str,
    *,
    max_precision: DatePrecision = "year",
) -> TemporalCheckResult:
    """Flag calendar detail in a reply when only coarser time is known."""
    text = (reply or "").strip()
    if not text:
        return TemporalCheckResult(passed=True)

    flags: list[str] = []
    if max_precision == "year":
        if _ISO_DATE.search(text):
            flags.append("false_precision_iso_date")
        if _ISO_MONTH.search(text):
            flags.append("false_precision_iso_month")
        if _MONTH_NAME_DAY.search(text):
            flags.append("false_precision_named_month_day")
        if _SLASH_DATE.search(text):
            flags.append("false_precision_slash_date")
    elif max_precision == "month":
        if _ISO_DATE.search(text):
            flags.append("false_precision_iso_date")
        if _MONTH_NAME_DAY.search(text):
            flags.append("false_precision_named_month_day")
        if _SLASH_DATE.search(text):
            flags.append("false_precision_slash_date")

    return TemporalCheckResult(passed=not flags, flags=flags)


def check_chapter_headers_false_precision(
    memory_chapters: list[dict[str, Any]],
) -> TemporalCheckResult:
    """Bundle headers must not show day/month when chapter time_precision is year."""
    flags: list[str] = []
    for group in memory_chapters or []:
        header = (group.get("header") or "").strip()
        if not header:
            continue
        if "year precision" not in header.casefold():
            continue
        if _ISO_DATE.search(header):
            flags.append(f"header_iso_date:{header[:80]}")
        if re.search(r"\b20\d{2}-\d{2}\b", header) and "year precision" in header:
            # YYYY-MM in a year-precision chapter header
            flags.append(f"header_iso_month:{header[:80]}")
    return TemporalCheckResult(passed=not flags, flags=flags)


def check_bundle_validity_expectations(
    bundle: dict[str, Any],
    *,
    expect_intent: str | None = None,
    exclude_content_substrings: tuple[str, ...] = (),
    require_content_substrings: tuple[str, ...] = (),
) -> TemporalCheckResult:
    flags: list[str] = []
    prov = bundle.get("provenance") or {}
    intent = prov.get("temporal_intent")
    if expect_intent and intent != expect_intent:
        flags.append(f"intent_mismatch:expected={expect_intent}:got={intent}")

    header_check = check_chapter_headers_false_precision(bundle.get("memory_chapters") or [])
    flags.extend(header_check.flags)

    flat = " ".join(
        (m.get("content") or "") for m in bundle.get("relevant_memories") or []
    )
    for needle in exclude_content_substrings:
        if needle and needle in flat:
            flags.append(f"excluded_memory_present:{needle[:48]}")
    for needle in require_content_substrings:
        if needle and needle not in flat:
            flags.append(f"required_memory_missing:{needle[:48]}")

    return TemporalCheckResult(passed=not flags, flags=flags)
