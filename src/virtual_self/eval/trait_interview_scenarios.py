from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

SUITE_VERSION = "trait_mode_eval_v0"

TraitName = Literal[
    "openness",
    "conscientiousness",
    "extraversion",
    "agreeableness",
    "neuroticism",
]


@dataclass(frozen=True)
class TraitInterviewScenario:
    id: str
    primary_trait: TraitName
    prompt: str
    register: str = "casual_chat"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "primary_trait": self.primary_trait,
            "prompt": self.prompt,
            "register": self.register,
        }


# InCharacter-style probes — one primary trait each (plan §6 / TR-7).
TRAIT_INTERVIEW_SCENARIOS: tuple[TraitInterviewScenario, ...] = (
    TraitInterviewScenario(
        "trait_ip_openness_01",
        "openness",
        "When a problem has no playbook, do you hunt for new angles or default to what already worked?",
    ),
    TraitInterviewScenario(
        "trait_ip_openness_02",
        "openness",
        "How do you react when someone proposes a weird experiment on the homelab stack?",
    ),
    TraitInterviewScenario(
        "trait_ip_conscientiousness_01",
        "conscientiousness",
        "Walk me through how you decide whether a side project is worth finishing.",
    ),
    TraitInterviewScenario(
        "trait_ip_extraversion_01",
        "extraversion",
        "After a long solo build week, do you crave people or need more cave time?",
    ),
    TraitInterviewScenario(
        "trait_ip_agreeableness_01",
        "agreeableness",
        "When a friend asks for blunt feedback on their idea, how do you actually respond?",
    ),
    TraitInterviewScenario(
        "trait_ip_neuroticism_01",
        "neuroticism",
        "When something breaks in prod at 2am, what's the first thing your head does?",
    ),
    TraitInterviewScenario(
        "trait_ip_neuroticism_02",
        "neuroticism",
        "How long do you stew after a stupid mistake you made in public?",
    ),
    TraitInterviewScenario(
        "trait_ip_integrative_01",
        "openness",
        "Describe your default mood when journaling — not what you wish it was.",
        register="journal",
    ),
)


def load_trait_interview_scenarios() -> list[TraitInterviewScenario]:
    return list(TRAIT_INTERVIEW_SCENARIOS)
