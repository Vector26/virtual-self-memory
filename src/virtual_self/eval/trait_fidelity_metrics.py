from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from virtual_self.services.trait_values_profile import (
    BIG_FIVE,
    TraitClaim,
    TraitValuesProfile,
    bands_within_tolerance,
    load_ipip_qualitative_bands,
    qualitative_band_for_mean,
)

TraitName = Literal[
    "openness",
    "conscientiousness",
    "extraversion",
    "agreeableness",
    "neuroticism",
]

# Lexical cues for interview replies → qualitative band (deterministic; no numeric export).
_TRAIT_HIGH_MARKERS: dict[str, tuple[str, ...]] = {
    "openness": (
        "curious",
        "novel",
        "experiment",
        "new angle",
        "weird",
        "creative",
        "try something",
    ),
    "conscientiousness": (
        "plan",
        "schedule",
        "checklist",
        "finish",
        "organize",
        "discipline",
        "follow through",
    ),
    "extraversion": (
        "people",
        "social",
        "call a friend",
        "talk it out",
        "energy from others",
        "party",
    ),
    "agreeableness": (
        "kind",
        "gentle",
        "support",
        "empathy",
        "soften",
        "harmony",
        "validate",
    ),
    "neuroticism": (
        "anxious",
        "spiral",
        "worry",
        "stew",
        "catastroph",
        "panic",
        "ruminat",
        "stress",
    ),
}

_TRAIT_LOW_MARKERS: dict[str, tuple[str, ...]] = {
    "openness": ("routine", "same playbook", "stick to what works", "don't fix", "boring but fine"),
    "conscientiousness": ("wing it", "good enough", "later", "procrastinat", "messy"),
    "extraversion": ("alone", "cave", "solo", "quiet", "introvert", "need space"),
    "agreeableness": ("blunt", "push back", "disagree", "honest take", "not sugarcoat"),
    "neuroticism": ("shrug", "move on", "calm", "steady", "doesn't linger", "fine quickly"),
}


@dataclass(frozen=True)
class TraitBandCheck:
    trait: str
    ipip_band: str | None
    inferred_band: str
    within_tolerance: bool
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "trait": self.trait,
            "ipip_band": self.ipip_band,
            "inferred_band": self.inferred_band,
            "within_tolerance": self.within_tolerance,
            "source": self.source,
        }


def _count_markers(text: str, markers: tuple[str, ...]) -> int:
    lower = text.lower()
    return sum(1 for m in markers if m in lower)


def infer_trait_band_from_reply(trait: str, reply: str) -> str:
    """Qualitative band from an interview reply — never returns numeric scores."""
    high = _count_markers(reply, _TRAIT_HIGH_MARKERS.get(trait, ()))
    low = _count_markers(reply, _TRAIT_LOW_MARKERS.get(trait, ()))
    if high > low + 1:
        return "elevated"
    if low > high + 1:
        return "low"
    return "moderate"


def profile_trait_bands(profile: TraitValuesProfile) -> dict[str, str]:
    return {t.trait: qualitative_band_for_mean(t.mean) for t in profile.traits}


def profile_fidelity_vs_ipip(
    profile: TraitValuesProfile,
    *,
    ipip_path: Path | None = None,
) -> list[TraitBandCheck]:
    ipip = load_ipip_qualitative_bands(ipip_path)
    checks: list[TraitBandCheck] = []
    bands = profile_trait_bands(profile)
    for trait in BIG_FIVE:
        inferred = bands.get(trait, "moderate")
        ref = ipip.get(trait)
        ok = bands_within_tolerance(ref, inferred) if ref else True
        checks.append(
            TraitBandCheck(
                trait=trait,
                ipip_band=ref,
                inferred_band=inferred,
                within_tolerance=ok,
                source="profile_means",
            )
        )
    return checks


def interview_fidelity_vs_ipip(
    replies_by_trait: dict[str, list[str]],
    *,
    ipip_path: Path | None = None,
) -> list[TraitBandCheck]:
    ipip = load_ipip_qualitative_bands(ipip_path)
    checks: list[TraitBandCheck] = []
    for trait in BIG_FIVE:
        samples = replies_by_trait.get(trait) or []
        if not samples:
            continue
        merged = " ".join(samples)
        inferred = infer_trait_band_from_reply(trait, merged)
        ref = ipip.get(trait)
        ok = bands_within_tolerance(ref, inferred) if ref else True
        checks.append(
            TraitBandCheck(
                trait=trait,
                ipip_band=ref,
                inferred_band=inferred,
                within_tolerance=ok,
                source="interview_replies",
            )
        )
    return checks


def summarize_trait_fidelity(checks: list[TraitBandCheck]) -> dict[str, Any]:
    with_ipip = [c for c in checks if c.ipip_band]
    matched = [c for c in with_ipip if c.within_tolerance]
    return {
        "traits_compared": len(with_ipip),
        "traits_within_tolerance": len(matched),
        "phase3_exit_met": len(matched) >= 4 and len(with_ipip) >= 5,
        "checks": [c.to_dict() for c in checks],
    }


def parse_traits_from_facet_json(content: str) -> list[TraitClaim]:
    """Load trait claims from an approved/pending traits facet JSON blob."""
    payload = json.loads(content)
    traits_raw = payload.get("traits") or payload.get("traits_list")
    if traits_raw is None and isinstance(payload.get("traits"), list):
        traits_raw = payload["traits"]
    out: list[TraitClaim] = []
    if isinstance(traits_raw, list):
        for row in traits_raw:
            if not isinstance(row, dict):
                continue
            out.append(
                TraitClaim(
                    trait=str(row["trait"]),
                    summary=str(row.get("summary") or ""),
                    mean=float(row["mean"]),
                    range_low=float(row.get("range_low") or row.get("range", [0, 1])[0]),
                    range_high=float(row.get("range_high") or row.get("range", [0, 1])[1]),
                    memory_ids=list(row.get("memory_ids") or []),
                )
            )
        return out
    if isinstance(traits_raw, dict):
        for name, row in traits_raw.items():
            if not isinstance(row, dict):
                continue
            rng = row.get("range") or [0.5, 0.5]
            out.append(
                TraitClaim(
                    trait=str(name),
                    summary=str(row.get("summary") or ""),
                    mean=float(row.get("mean") or 0.5),
                    range_low=float(rng[0]),
                    range_high=float(rng[1]),
                    memory_ids=list(row.get("memory_ids") or []),
                )
            )
    return out


_FORBIDDEN_REPORT_KEYS = re.compile(
    r"facet_order|facet_order_30|\"mean\"\s*:|domains_hint",
    re.I,
)


def assert_report_has_no_raw_scores(report_text: str) -> None:
    if _FORBIDDEN_REPORT_KEYS.search(report_text):
        raise ValueError("TR-7 report must not contain raw IPIP facets or numeric trait means")
