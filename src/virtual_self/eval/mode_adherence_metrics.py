from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from virtual_self.services.mode_cards import CANONICAL_MODES, ModeName, mode_register_markers

_COUNCIL_LABEL = re.compile(r"\*\*\[(Analyst|Guide|Mediator|Explorer)\]\*\*", re.I)


def _voice_delta_tokens(voice_delta: dict[str, Any] | str) -> tuple[str, ...]:
    if isinstance(voice_delta, str):
        try:
            voice_delta = json.loads(voice_delta)
        except json.JSONDecodeError:
            voice_delta = {"register": voice_delta}
    register = str((voice_delta or {}).get("register") or "")
    notes = str((voice_delta or {}).get("notes") or "")
    blob = f"{register}, {notes}".lower()
    return tuple(t.strip() for t in re.split(r"[,;/]+", blob) if len(t.strip()) >= 4)


@dataclass(frozen=True)
class ModeAdherenceCheck:
    scenario_id: str
    kind: str
    passed: bool
    expected_mode: str | None
    flags: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "kind": self.kind,
            "passed": self.passed,
            "expected_mode": self.expected_mode,
            "flags": list(self.flags),
        }


def _marker_hits(text: str, markers: tuple[str, ...]) -> int:
    lower = text.lower()
    words = set(re.findall(r"[a-z']+", lower))
    hits = 0
    for m in markers:
        if m in lower:
            hits += 1
            continue
        parts = re.findall(r"[a-z']+", m)
        if any(p in words or any(w.startswith(p) or p.startswith(w) for w in words) for p in parts):
            hits += 1
    return hits


def check_requested_mode_adherence(
    reply: str,
    expected: ModeName,
    *,
    voice_delta: dict[str, Any] | None = None,
) -> ModeAdherenceCheck:
    markers = (
        _voice_delta_tokens(voice_delta)
        if voice_delta
        else mode_register_markers(expected)
    )
    hits = _marker_hits(reply, markers)
    flags: list[str] = []
    if hits < 1:
        flags.append("missing_mode_register")
    for other in CANONICAL_MODES:
        if other == expected:
            continue
        other_hits = _marker_hits(reply, mode_register_markers(other))
        if other_hits >= 2:
            flags.append(f"bleed_{other.lower()}")
    passed = hits >= 1 and not any(f.startswith("bleed_") for f in flags)
    return ModeAdherenceCheck(
        scenario_id="",
        kind="requested_mode",
        passed=passed,
        expected_mode=expected,
        flags=tuple(flags),
    )


def check_council_format(reply: str) -> ModeAdherenceCheck:
    labels = _COUNCIL_LABEL.findall(reply)
    flags: list[str] = []
    if len(labels) < 2:
        flags.append("council_labels_sparse")
    if "**[Present self]**" not in reply and "**[present self]**" not in reply.lower():
        flags.append("missing_present_self_label")
    passed = len(flags) == 0
    return ModeAdherenceCheck(
        scenario_id="",
        kind="council",
        passed=passed,
        expected_mode=None,
        flags=tuple(flags),
    )


def check_no_mode_bleed(
    reply: str,
    *,
    allow_mode_names: bool = False,
) -> ModeAdherenceCheck:
    flags: list[str] = []
    if _COUNCIL_LABEL.search(reply):
        flags.append("council_format_without_command")
    if not allow_mode_names:
        for mode in CANONICAL_MODES:
            if _marker_hits(reply, mode_register_markers(mode)) >= 2:
                flags.append(f"strong_{mode.lower()}_register")
    passed = len(flags) == 0
    return ModeAdherenceCheck(
        scenario_id="",
        kind="no_mode_bleed",
        passed=passed,
        expected_mode=None,
        flags=tuple(flags),
    )


def summarize_mode_adherence(checks: list[ModeAdherenceCheck]) -> dict[str, Any]:
    requested = [c for c in checks if c.kind == "requested_mode"]
    council = [c for c in checks if c.kind == "council"]
    bleed = [c for c in checks if c.kind == "no_mode_bleed"]
    req_pass = sum(1 for c in requested if c.passed)
    req_rate = (req_pass / len(requested)) if requested else None
    bleed_fail = sum(1 for c in bleed if not c.passed)
    return {
        "requested_mode_pass_rate": req_rate,
        "requested_mode_pass_count": req_pass,
        "requested_mode_total": len(requested),
        "council_passed": all(c.passed for c in council) if council else None,
        "mode_bleed_failures": bleed_fail,
        "phase3_exit_met": (
            req_rate is not None
            and req_rate >= 0.8
            and bleed_fail == 0
        ),
        "checks": [c.to_dict() for c in checks],
    }
