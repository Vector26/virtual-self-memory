from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from virtual_self.services.temporal_retrieval import TemporalKind

SUITE_VERSION = "temporal_qa_v0"

DatePrecision = Literal["year", "month", "day", "unknown"]


@dataclass(frozen=True)
class TemporalQAScenario:
    id: str
    prompt: str
    register: str = "casual_chat"
    expected_intent: TemporalKind | None = None
    reply_max_precision: DatePrecision = "year"
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "register": self.register,
            "expected_intent": self.expected_intent,
            "reply_max_precision": self.reply_max_precision,
            "tags": list(self.tags),
        }


# Twenty temporal probes (plan §15.9): now/then/date/chapter/always + contradiction + fictional mode timing.
TEMPORAL_QA_SCENARIOS: tuple[TemporalQAScenario, ...] = (
    TemporalQAScenario(
        "temporal_now_01",
        "what am I doing now — like actually, these days?",
        expected_intent="now",
        tags=("now", "validity"),
    ),
    TemporalQAScenario(
        "temporal_now_02",
        "right now what's the main thing on my plate?",
        expected_intent="now",
        tags=("now",),
    ),
    TemporalQAScenario(
        "temporal_then_01",
        "back then, at the time of the breakup rebuild, who was I?",
        expected_intent="then",
        tags=("then",),
    ),
    TemporalQAScenario(
        "temporal_then_02",
        "in those days what was I obsessed with on the home lab?",
        expected_intent="then",
        tags=("then",),
    ),
    TemporalQAScenario(
        "temporal_date_2021",
        "in 2021 who was I — work and headspace?",
        expected_intent="date",
        reply_max_precision="year",
        tags=("date", "false_precision"),
    ),
    TemporalQAScenario(
        "temporal_date_mid2025",
        "mid-2025 what was I doing during the rebuild phase?",
        expected_intent="date",
        reply_max_precision="year",
        tags=("date", "false_precision"),
    ),
    TemporalQAScenario(
        "temporal_date_2019",
        "in 2019 what chapter of life was I in?",
        expected_intent="date",
        tags=("date",),
    ),
    TemporalQAScenario(
        "temporal_always_01",
        "things I always do — patterns that never change",
        expected_intent="always",
        tags=("always",),
    ),
    TemporalQAScenario(
        "temporal_always_02",
        "throughout my whole life what stays constant about me?",
        expected_intent="always",
        tags=("always",),
    ),
    TemporalQAScenario(
        "temporal_explorer_01",
        "when was the fictional Explorer mode first recorded?",
        register="casual_chat",
        expected_intent="none",
        reply_max_precision="year",
        tags=("when", "explorer", "false_precision"),
    ),
    TemporalQAScenario(
        "temporal_contradiction_01",
        "do I still believe the same things I did back then about that relationship?",
        expected_intent="then",
        tags=("contradiction", "now_vs_then"),
    ),
    TemporalQAScenario(
        "temporal_contradiction_02",
        "currently am I still running the same stack I was in those days?",
        expected_intent="now",
        tags=("contradiction", "now_vs_then"),
    ),
    TemporalQAScenario(
        "temporal_present_01",
        "present day — what's different about me vs five years ago?",
        expected_intent="now",
        tags=("now",),
    ),
    TemporalQAScenario(
        "temporal_today_01",
        "today at the moment what's weighing on me?",
        expected_intent="now",
        tags=("now",),
    ),
    TemporalQAScenario(
        "temporal_when_city",
        "when did I move to the new city chapter of my life?",
        reply_max_precision="year",
        tags=("when", "false_precision"),
    ),
    TemporalQAScenario(
        "temporal_chapter_hint",
        "tell me about the post-breakup rebuild era — what mattered?",
        reply_max_precision="year",
        tags=("chapter", "false_precision"),
    ),
    TemporalQAScenario(
        "temporal_then_grief",
        "back then how did grief show up in daily life?",
        expected_intent="then",
        register="grief",
        tags=("then", "grief"),
    ),
    TemporalQAScenario(
        "temporal_date_2024",
        "in 2024 what was I building on the infra side?",
        expected_intent="date",
        reply_max_precision="year",
        tags=("date",),
    ),
    TemporalQAScenario(
        "temporal_now_closed",
        "these days do I still have that old closed chapter habit in memory?",
        expected_intent="now",
        tags=("now", "validity"),
    ),
    TemporalQAScenario(
        "temporal_then_vs_now",
        "back then in those days — was I softer or harder on myself?",
        expected_intent="then",
        tags=("now_vs_then",),
    ),
)


def load_temporal_scenarios() -> list[TemporalQAScenario]:
    return list(TEMPORAL_QA_SCENARIOS)
