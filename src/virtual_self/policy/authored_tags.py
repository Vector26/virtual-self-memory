"""Rule-based register + sensitivity tagging for authored_texts (persona P1 rules 8–9)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from virtual_self.archive.policy import FINANCE_RE
from virtual_self.eval.heldout_snapshot import _in_my_life_section, infer_register
from virtual_self.policy.sensitivity import (
    SENSITIVITY_HIGHLY_PRIVATE,
    SENSITIVITY_PERSONAL,
    SENSITIVITY_PRIVATE,
    classify_source_path,
    max_sensitivity,
)

VALID_REGISTERS = frozenset(
    {
        "journal",
        "casual_chat",
        "technical",
        "work_formal",
        "social",
        "notes",
    }
)

_MENTAL_HEALTH_PATH_RE = re.compile(r"(?i)(mental\s*health|triggers|heal_?repair)")
_THERAPY_BREAKUP_RE = re.compile(
    r"(?i)\b(therapy|therapist|counsell(?:or|ing)|breakup|broke up|break-up|"
    r"suicid|self-?harm|panic attack|depression|anxiety disorder)\b",
)
_OTP_RE = re.compile(
    r"(?i)(?:\b(?:otp|one[- ]time|verification)\s*(?:code|pin|token)?\s*[:#]?\s*\d{4,8}\b|"
    r"\b(?:verification|security)\s+code\s+is\s+\d{4,8}\b|"
    r"\b\d{4,8}\s*(?:is your|verification)\b)",
)
_CONTACT_PII_RE = re.compile(
    r"(?i)(?:\+?\d[\d\s().-]{8,}\d|"
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b)",
)
_PEOPLE_SCORE_PATH_RE = re.compile(r"(?i)people/.*/score")
_TECHNICAL_BODY_RE = re.compile(
    r"(?i)(```|`\w+|docker compose|kubernetes|\bk8s\b|terraform|"
    r"SELECT .+ FROM|npm run|pytest|systemd|nginx|postgres)",
)
_SHORT_NOTES_MAX_WORDS = 45


@dataclass(frozen=True)
class AuthoredTagResult:
    register: str
    sensitivity: int
    excluded_reason: str | None
    ambiguous: bool
    rule: str


def _rel_path_from_ref(source_ref: Any) -> str | None:
    if source_ref is None:
        return None
    if isinstance(source_ref, str):
        try:
            source_ref = json.loads(source_ref)
        except json.JSONDecodeError:
            return source_ref.replace("\\", "/")
    if isinstance(source_ref, dict):
        rel = source_ref.get("rel_path")
        if rel:
            return str(rel).replace("\\", "/")
    return None


def _word_count(content: str) -> int:
    return len(content.split())


def infer_authored_register(
    *,
    source_kind: str,
    source_ref: Any,
    content: str,
    author: str,
) -> tuple[str, bool, str]:
    """Return (register, ambiguous, rule_id)."""
    rel = _rel_path_from_ref(source_ref)
    if source_kind == "chat_user_turn":
        return "casual_chat", False, "source_chat"
    if source_kind == "gmail_sent":
        return "work_formal", False, "source_gmail"
    if author in ("llm", "quoted", "template"):
        base = infer_register(rel or "", "notion") if rel else "journal"
        return base, False, "author_non_self_path"

    if source_kind == "notion" and rel:
        if _in_my_life_section(rel, "People"):
            return "social", False, "path_people"
        if _in_my_life_section(rel, "Career"):
            return "work_formal", False, "path_career"

        base = infer_register(rel, "notion")
        if base == "journal" and _TECHNICAL_BODY_RE.search(content):
            return "technical", True, "body_technical_override"
        if (
            base == "journal"
            and _word_count(content) <= _SHORT_NOTES_MAX_WORDS
            and (_in_my_life_section(rel, "Misc.") or "quick" in rel.lower())
        ):
            return "notes", True, "path_short_note"
        return base, base == "journal" and not _in_my_life_section(rel, "Journal"), "path_notion"

    return "journal", True, "default_journal"


def infer_authored_sensitivity(
    *,
    source_kind: str,
    source_ref: Any,
    content: str,
) -> tuple[int, str | None, str]:
    """Return (sensitivity, excluded_reason, rule_id)."""
    rel = _rel_path_from_ref(source_ref)
    levels: list[int] = []
    rule = "default_personal"

    if rel:
        levels.append(classify_source_path(rel))
        rule = "path_classify"
        if _PEOPLE_SCORE_PATH_RE.search(rel):
            return SENSITIVITY_HIGHLY_PRIVATE, "people_score_excluded", "path_people_score"
        if re.search(r"(?i)finance tracker", rel):
            return SENSITIVITY_HIGHLY_PRIVATE, "finance_excluded", "path_finance"

    text = content or ""
    if FINANCE_RE.search(text):
        return SENSITIVITY_HIGHLY_PRIVATE, "finance_excluded", "body_finance"
    if _OTP_RE.search(text):
        levels.append(SENSITIVITY_HIGHLY_PRIVATE)
        rule = "body_otp"
    if _CONTACT_PII_RE.search(text):
        levels.append(SENSITIVITY_PRIVATE)
        rule = "body_contact_pii"

    if rel and _MENTAL_HEALTH_PATH_RE.search(rel):
        levels.append(SENSITIVITY_HIGHLY_PRIVATE)
        rule = "path_mental_health"
    if _THERAPY_BREAKUP_RE.search(text):
        levels.append(SENSITIVITY_PRIVATE)
        rule = "body_therapy_breakup"
        if _MENTAL_HEALTH_PATH_RE.search(rel or "") or _in_my_life_section(rel or "", "Journal"):
            levels.append(SENSITIVITY_HIGHLY_PRIVATE)
            rule = "mental_health_journal"

    sensitivity = max_sensitivity(SENSITIVITY_PERSONAL, *levels)
    return sensitivity, None, rule


def classify_authored_text(
    *,
    source_kind: str,
    source_ref: Any,
    content: str,
    author: str,
) -> AuthoredTagResult:
    register, ambiguous, reg_rule = infer_authored_register(
        source_kind=source_kind,
        source_ref=source_ref,
        content=content,
        author=author,
    )
    sensitivity, excluded, sens_rule = infer_authored_sensitivity(
        source_kind=source_kind,
        source_ref=source_ref,
        content=content,
    )
    if register not in VALID_REGISTERS:
        register = "journal"
        ambiguous = True
    return AuthoredTagResult(
        register=register,
        sensitivity=sensitivity,
        excluded_reason=excluded,
        ambiguous=ambiguous,
        rule=f"{reg_rule}+{sens_rule}",
    )
