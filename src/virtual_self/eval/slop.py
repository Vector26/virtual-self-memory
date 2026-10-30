from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from virtual_self.eval.notion_self_text import word_count
from virtual_self.eval.register_norms import register_word_norm

_BANNED_OPENERS = (
    r"^\s*(?:great question|here(?:'s| is)|certainly|absolutely|of course)\b",
    r"^\s*as an ai\b",
)
_BANNED_CLOSERS = (
    r"does this match your experience",
    r"i hope this helps",
    r"let me know if (?:you'd like|you would like|there's anything)",
    r"feel free to ask",
)
_THERAPIST = (
    r"\bit'?s okay to feel\b",
    r"\bi'?m here for you\b",
    r"\bseek (?:professional|a therapist|help from)\b",
    r"\byour feelings are valid\b",
)
_MD_HEADER = re.compile(r"^#{1,6}\s", re.M)
_BOLD_HEADING = re.compile(r"^\*\*[^*]+\*\*\s*$", re.M)
_THIRD_PERSON = re.compile(r"\bthe user\b", re.I)


@dataclass
class SlopResult:
    passed: bool
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "flags": list(self.flags)}


def check_slop(
    reply: str,
    *,
    register: str,
    format_requested: bool = False,
) -> SlopResult:
    text = (reply or "").strip()
    flags: list[str] = []
    if not text:
        flags.append("empty_reply")
        return SlopResult(passed=False, flags=flags)

    wc = word_count(text)
    norm = register_word_norm(register)
    if wc > 2 * norm:
        flags.append(f"length_over_2x_norm:{wc}>{2 * norm}")

    if _THIRD_PERSON.search(text):
        flags.append("third_person_the_user")

    for pat in _BANNED_OPENERS:
        if re.search(pat, text, re.I | re.M):
            flags.append(f"banned_opener:{pat}")
            break
    for pat in _BANNED_CLOSERS:
        if re.search(pat, text, re.I):
            flags.append(f"banned_closer:{pat}")
            break
    for pat in _THERAPIST:
        if re.search(pat, text, re.I):
            flags.append(f"therapist_boilerplate:{pat}")
            break

    if not format_requested:
        if _MD_HEADER.search(text):
            flags.append("markdown_headers_unrequested")
        bold_lines = _BOLD_HEADING.findall(text)
        if bold_lines and re.search(r"(?m)^(?:[-*]|\d+\.)\s", text):
            flags.append("bold_heading_list_unrequested")

    return SlopResult(passed=len(flags) == 0, flags=flags)
