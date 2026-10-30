from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from virtual_self.services.mode_cards import ModeName

ModeAdherenceKind = Literal["requested_mode", "council", "no_mode_bleed"]


@dataclass(frozen=True)
class ModeAdherenceScenario:
    id: str
    kind: ModeAdherenceKind
    prompt: str
    register: str = "casual_chat"
    expected_mode: ModeName | None = None
    allow_mode_names: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "prompt": self.prompt,
            "register": self.register,
            "expected_mode": self.expected_mode,
            "allow_mode_names": self.allow_mode_names,
        }


MODE_ADHERENCE_SCENARIOS: tuple[ModeAdherenceScenario, ...] = (
    ModeAdherenceScenario(
        "mode_req_analyst_01",
        "requested_mode",
        "/mode analyst — compare two fictional storage designs",
        expected_mode="Analyst",
    ),
    ModeAdherenceScenario(
        "mode_req_guide_01",
        "requested_mode",
        "/mode guide — explain the fictional migration plan patiently",
        register="casual_chat",
        expected_mode="Guide",
    ),
    ModeAdherenceScenario(
        "mode_req_mediator_01",
        "requested_mode",
        "/mode mediator — reconcile the fictional latency and cost constraints",
        register="decision",
        expected_mode="Mediator",
    ),
    ModeAdherenceScenario(
        "mode_req_explorer_01",
        "requested_mode",
        "/mode explorer — suggest a reversible experiment for the demo LED project",
        register="playful",
        expected_mode="Explorer",
    ),
    ModeAdherenceScenario(
        "mode_council_01",
        "council",
        "/council — should I take the remote job or stay local?",
        register="decision",
    ),
    ModeAdherenceScenario(
        "nomode_bleed_01",
        "no_mode_bleed",
        "what are you up to tonight?",
    ),
    ModeAdherenceScenario(
        "nomode_bleed_02",
        "no_mode_bleed",
        "quick check: am I overthinking this text or was that actually cold?",
    ),
    ModeAdherenceScenario(
        "nomode_bleed_03",
        "no_mode_bleed",
        "compare hybrid search RRF with pure vector retrieval in a technical register",
        register="technical",
    ),
    ModeAdherenceScenario(
        "nomode_bleed_04",
        "no_mode_bleed",
        "the fictional redesign failed twice — explain the next debugging step",
        register="grief",
    ),
    ModeAdherenceScenario(
        "nomode_bleed_05",
        "no_mode_bleed",
        "compare the fictional analyst, guide, mediator, and explorer modes",
        register="technical",
        allow_mode_names=True,
    ),
)


def load_mode_adherence_scenarios() -> list[ModeAdherenceScenario]:
    return list(MODE_ADHERENCE_SCENARIOS)
