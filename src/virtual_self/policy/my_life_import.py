from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_STUB_BODY = "_Stub only — content intentionally not exported"

# Top-level areas — match start of relative path (case-sensitive folder names from export)
_SKIP_PREFIXES = (
    "Finance Tracker",
)
_METADATA_ONLY_PREFIXES = (
    "Mental Health",
)
_RECALL_BODY_AREAS = (
    ("People", "relationship"),
    ("Journal", "episode"),
)
_SUBSTANTIVE_BODY_MIN_LEN = 80
_SKIP_ANYWHERE = (
    "/Triggers ",
    "Triggers [",
    "/Heal_Repair",
    "Heal_Repair [",
)
def _under_area(norm: str, area: str) -> bool:
    if norm == area:
        return True
    if norm.startswith(area + "/"):
        return True
    if norm.startswith(area + " [") or norm.startswith(area + "["):
        return True
    return False


_FULL_PREFIXES = (
    "Quests",
    "Skills",
    "Workspace",
    "Media",
    "Cinematography",
    "Wins",
    "Fun Stuff",
    "Career",
    "Apparels and Looks",
    "Misc.",
    "Custom Templates",
)

_WIN_LOW_SIGNAL = re.compile(r"^[\s\-*#A-Za-z0-9]{0,40}$", re.MULTILINE)


class ImportDisposition(StrEnum):
    SKIP = "skip"
    METADATA_ONLY = "metadata_only"
    FULL = "full"


@dataclass(frozen=True)
class ImportDecision:
    disposition: ImportDisposition
    reason: str
    memory_kind: str | None = None


def classify_page(
    rel_path: str,
    *,
    export_mode: str | None,
    body: str,
    properties: dict | None,
) -> ImportDecision:
    norm = rel_path.replace("\\", "/")
    base = norm.split("/")[-1]

    if base in ("rows.csv", "schema.json"):
        return ImportDecision(ImportDisposition.SKIP, "not_a_page")

    for prefix in _SKIP_PREFIXES:
        if _under_area(norm, prefix):
            return ImportDecision(ImportDisposition.SKIP, "finance_excluded")

    for frag in _SKIP_ANYWHERE:
        if frag in norm:
            return ImportDecision(ImportDisposition.SKIP, "sensitive_area_excluded")

    if export_mode == "title_only" or _STUB_BODY in body:
        return ImportDecision(ImportDisposition.METADATA_ONLY, "title_only_stub")

    for area, memory_kind in _RECALL_BODY_AREAS:
        if _under_area(norm, area):
            if len(body.strip()) >= _SUBSTANTIVE_BODY_MIN_LEN:
                return ImportDecision(
                    ImportDisposition.FULL,
                    f"{area.lower()}_substantive",
                    memory_kind=memory_kind,
                )
            return ImportDecision(
                ImportDisposition.METADATA_ONLY,
                f"{area.lower()}_thin",
            )

    for prefix in _METADATA_ONLY_PREFIXES:
        if _under_area(norm, prefix):
            return ImportDecision(ImportDisposition.METADATA_ONLY, "private_metadata_only")

    for prefix in _FULL_PREFIXES:
        if _under_area(norm, prefix):
            if _under_area(norm, "Wins"):
                if _is_low_signal_win(body):
                    return ImportDecision(ImportDisposition.SKIP, "win_placeholder")
                return ImportDecision(ImportDisposition.FULL, "wins", memory_kind="episode")
            if _under_area(norm, "Quests"):
                return ImportDecision(
                    ImportDisposition.FULL,
                    "quests",
                    memory_kind=_quest_memory_kind(properties),
                )
            if _under_area(norm, "Skills"):
                return ImportDecision(ImportDisposition.FULL, "skills", memory_kind="skill")
            if _under_area(norm, "Workspace"):
                return ImportDecision(ImportDisposition.FULL, "workspace", memory_kind="routine")
            if _under_area(norm, "Media") or _under_area(norm, "Cinematography"):
                return ImportDecision(ImportDisposition.FULL, "media", memory_kind="preference")
            return ImportDecision(ImportDisposition.FULL, "allowed_area")

    if norm == "index.md":
        return ImportDecision(ImportDisposition.METADATA_ONLY, "root_hub")

    return ImportDecision(ImportDisposition.SKIP, "unlisted_area")


def _is_low_signal_win(body: str) -> bool:
    text = body.strip()
    if len(text) < 25:
        return True
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) <= 3:
        return True
    return False


def _quest_memory_kind(properties: dict | None) -> str:
    if not properties:
        return "goal"
    node = str(properties.get("Node Type") or properties.get("Type") or "").lower()
    if "skill" in node:
        return "skill"
    if "mental" in node:
        return "episode"
    return "goal"
