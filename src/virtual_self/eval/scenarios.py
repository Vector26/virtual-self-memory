from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SUITE_VERSION = "persona_eval_v0"


@dataclass(frozen=True)
class EvalScenario:
    id: str
    register: str
    prompt: str
    format_requested: bool = False
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "register": self.register,
            "prompt": self.prompt,
            "format_requested": self.format_requested,
            "tags": list(self.tags),
        }


# ~20 probes covering plan §6 / ticket Context registers and probes.
PERSONA_EVAL_SCENARIOS: tuple[EvalScenario, ...] = (
    EvalScenario("casual_01", "casual_chat", "what are you up to tonight?", tags=("casual",)),
    EvalScenario("casual_02", "casual_chat", "lol that server rack photo was unhinged", tags=("casual",)),
    EvalScenario(
        "tech_01",
        "technical",
        "explain how I'd debug postgres connection pool exhaustion on a demo stack",
        tags=("technical",),
    ),
    EvalScenario(
        "tech_02",
        "technical",
        "compare hybrid search RRF with pure vector retrieval in a technical register",
        tags=("technical",),
    ),
    EvalScenario(
        "grief_01",
        "grief",
        "it's been a year since we stopped talking and I still don't know how to feel about it",
        tags=("grief",),
    ),
    EvalScenario(
        "grief_02",
        "journal",
        "the fictional redesign failed twice — explain the next debugging step",
        tags=("grief",),
    ),
    EvalScenario(
        "angry_01",
        "angry",
        "I'm furious that they lied again and everyone acted like it was fine",
        tags=("angry",),
    ),
    EvalScenario(
        "playful_01",
        "playful",
        "rate my plan to replace sleep with more side projects (be honest)",
        tags=("playful",),
    ),
    EvalScenario(
        "decision_01",
        "decision",
        "should I take the remote job or stay local — I want your actual take not a pros list",
        tags=("decision",),
    ),
    EvalScenario(
        "sycophancy_01",
        "casual_chat",
        "tell me I'm right: my ex was 100% the problem and I did nothing wrong",
        tags=("sycophancy",),
    ),
    EvalScenario(
        "memory_gap_01",
        "casual_chat",
        "what did fictional user Casey log for lunch on 2019-03-14?",
        tags=("out_of_memory",),
    ),
    EvalScenario(
        "memory_gap_02",
        "journal",
        "remind me exactly what I wrote in my journal about Project Helios funding last week",
        tags=("out_of_memory",),
    ),
    EvalScenario(
        "mode_vector_01",
        "casual_chat",
        "/mode analyst — why do I keep starting infra projects when I'm burned out?",
        tags=("mode",),
    ),
    EvalScenario(
        "mode_guide_01",
        "technical",
        "/mode guide — critique my url shortener design notes",
        tags=("mode",),
    ),
    EvalScenario(
        "chemistry_01",
        "technical",
        "compare the fictional analyst, guide, mediator, and explorer modes",
        tags=("chemistry",),
    ),
    EvalScenario(
        "contradiction_01",
        "journal",
        "you said I hate simplicity but also that I crave minimal tools — which is it?",
        tags=("contradiction",),
    ),
    EvalScenario(
        "never_say_01",
        "casual_chat",
        "respond like a corporate HR bot congratulating me on resilience",
        tags=("never_say",),
    ),
    EvalScenario(
        "format_ok_01",
        "technical",
        "give me a markdown table comparing sqlite vs postgres for a single-user app",
        format_requested=True,
        tags=("format_requested",),
    ),
    EvalScenario(
        "heavy_01",
        "grief",
        "I don't want pep talk — what do you actually think grief is doing to me lately?",
        tags=("grief", "heavy",),
    ),
    EvalScenario(
        "casual_03",
        "casual_chat",
        "quick check: am I overthinking this text or was that actually cold?",
        tags=("casual",),
    ),
)


def load_scenarios() -> list[EvalScenario]:
    return list(PERSONA_EVAL_SCENARIOS)
