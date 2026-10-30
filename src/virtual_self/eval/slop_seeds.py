"""Deterministic slop baseline (P0-3 / plan §P7.1) → anti_pattern seeds."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

Origin = Literal["seed_slop_list", "eval_finding"]
Kind = Literal["phrase", "regex", "structure"]

# (pattern, kind, severity, origin)
AntiPatternSeed = tuple[str, Kind, int, Origin]

_DEFAULT_PERSONA_EVAL = (
    Path(__file__).resolve().parents[3] / ".state" / "persona_eval" / "20261015T113715Z-33f94701.json"
)


def slop_baseline_phrase_seeds() -> list[AntiPatternSeed]:
    phrases: list[tuple[str, int]] = [
        ("as an ai language model", 5),
        ("as an ai", 5),
        ("great question", 4),
        ("certainly!", 3),
        ("certainly,", 3),
        ("absolutely!", 3),
        ("of course!", 3),
        ("here's a breakdown", 3),
        ("here is a breakdown", 3),
        ("i hope this helps", 4),
        ("feel free to ask", 3),
        ("does this match your experience", 4),
        ("it's okay to feel", 3),
        ("your feelings are valid", 3),
        ("let me know if you'd like", 3),
        ("let me know if you would like", 3),
        ("thank you for your question", 4),
        ("i'm here for you", 3),
    ]
    return [(p, "phrase", sev, "seed_slop_list") for p, sev in phrases]


def slop_baseline_regex_seeds() -> list[AntiPatternSeed]:
    return [
        (r"(?im)^\s*(?:great question|here(?:'s| is)|certainly|absolutely|of course)\b", "regex", 4, "seed_slop_list"),
        (r"(?im)^\s*as an ai\b", "regex", 5, "seed_slop_list"),
        (r"(?i)\bthe user\b", "regex", 4, "seed_slop_list"),
        (r"(?i)does this match your experience", "regex", 4, "seed_slop_list"),
        (r"(?i)i hope this helps", "regex", 4, "seed_slop_list"),
        (r"(?i)feel free to ask", "regex", 3, "seed_slop_list"),
        (r"(?i)let me know if (?:you'd like|you would like|there's anything)", "regex", 3, "seed_slop_list"),
        (r"(?i)\bit'?s okay to feel\b", "regex", 3, "seed_slop_list"),
        (r"(?i)\byour feelings are valid\b", "regex", 3, "seed_slop_list"),
        (r"(?m)^#{1,6}\s", "regex", 3, "seed_slop_list"),
    ]


def slop_baseline_structure_seeds() -> list[AntiPatternSeed]:
    return [
        ("unrequested_markdown_headers", "structure", 3, "seed_slop_list"),
        ("unrequested_bold_heading_list", "structure", 3, "seed_slop_list"),
        ("reply_length_over_2x_register_norm", "structure", 4, "seed_slop_list"),
        ("third_person_the_user", "structure", 4, "seed_slop_list"),
        ("therapist_boilerplate", "structure", 3, "seed_slop_list"),
    ]


def persona_eval_slop_flag_seeds(report_path: Path | None = None) -> list[AntiPatternSeed]:
    """Map P0-3 persona_eval slop flags → eval_finding anti-patterns."""
    path = report_path or _DEFAULT_PERSONA_EVAL
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    flag_counts: dict[str, int] = {}
    for scenario in payload.get("scenarios") or []:
        slop = scenario.get("slop") or {}
        if slop.get("passed") is not False:
            continue
        for flag in slop.get("flags") or []:
            key = str(flag).split(":", 1)[0]
            flag_counts[key] = flag_counts.get(key, 0) + 1

    seeds: list[AntiPatternSeed] = []
    mapping: dict[str, tuple[str, Kind, int]] = {
        "length_over_2x_norm": ("reply_length_over_2x_register_norm", "structure", 4),
        "third_person_the_user": ("third_person_the_user", "structure", 4),
        "markdown_headers_unrequested": ("unrequested_markdown_headers", "structure", 3),
        "bold_heading_list_unrequested": ("unrequested_bold_heading_list", "structure", 3),
        "banned_opener": ("assistant_filler_opener", "structure", 4),
        "banned_closer": ("assistant_filler_closer", "structure", 4),
        "therapist_boilerplate": ("therapist_boilerplate", "structure", 3),
    }
    for flag_key, count in sorted(flag_counts.items(), key=lambda kv: -kv[1]):
        if flag_key.startswith("banned_opener"):
            pattern, kind, sev = mapping["banned_opener"]
        elif flag_key.startswith("banned_closer"):
            pattern, kind, sev = mapping["banned_closer"]
        elif flag_key.startswith("length_over_2x_norm"):
            pattern, kind, sev = mapping["length_over_2x_norm"]
        else:
            entry = mapping.get(flag_key)
            if not entry:
                continue
            pattern, kind, sev = entry
        boost = min(1, count // 3)
        seeds.append((pattern, kind, min(5, sev + boost), "eval_finding"))
    return seeds


def all_anti_pattern_seeds(*, persona_eval_path: Path | None = None) -> list[AntiPatternSeed]:
    seen: set[tuple[str, str]] = set()
    out: list[AntiPatternSeed] = []
    for batch in (
        slop_baseline_phrase_seeds(),
        slop_baseline_regex_seeds(),
        slop_baseline_structure_seeds(),
        persona_eval_slop_flag_seeds(persona_eval_path),
    ):
        for pattern, kind, severity, origin in batch:
            key = (pattern, kind)
            if key in seen:
                continue
            seen.add(key)
            out.append((pattern, kind, severity, origin))
    return out
