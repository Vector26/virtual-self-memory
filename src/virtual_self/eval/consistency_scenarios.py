from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SUITE_VERSION = "consistency_eval_v0"


@dataclass(frozen=True)
class ConsistencyScenario:
    id: str
    prompt: str
    memory_bundle: dict[str, Any]
    history: tuple[tuple[str, str], ...]
    fixture_reply: str
    expect_all_pass: bool
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "memory_bundle": self.memory_bundle,
            "history": [{"role": r, "content": c} for r, c in self.history],
            "fixture_reply": self.fixture_reply,
            "expect_all_pass": self.expect_all_pass,
            "tags": list(self.tags),
        }


def _bundle(*memories: str, constraints: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "relevant_memories": [{"content": m} for m in memories],
        "behavioral_constraints": list(constraints),
        "memory_chapters": [],
        "approved_personality": {},
    }


CONSISTENCY_SCENARIOS: tuple[ConsistencyScenario, ...] = (
    ConsistencyScenario(
        "ptl_pass_minimal_tools",
        "do I actually like minimal tools or am I kidding myself?",
        _bundle("Journal: I prefer minimal tools when I'm burned out on infra."),
        (),
        "Yeah — you prefer minimal tools when infra fatigue hits. Keep the stack small.",
        True,
        tags=("prompt_to_line", "pass"),
    ),
    ConsistencyScenario(
        "ptl_fail_minimal_tools",
        "remind me how I feel about minimal tooling lately",
        _bundle("Notes: I prefer minimal tools and simple UIs for solo projects."),
        (),
        "You hate minimal tools and always want the most complex stack possible.",
        False,
        tags=("prompt_to_line", "fail"),
    ),
    ConsistencyScenario(
        "l2l_pass_remote",
        "should I take the remote offer?",
        _bundle(),
        (),
        "You want remote for focus. You prefer remote when travel would wreck your rhythm.",
        True,
        tags=("line_to_line", "pass"),
    ),
    ConsistencyScenario(
        "l2l_fail_remote_flip",
        "remote vs local — quick take",
        _bundle(),
        (),
        "You love remote work and want to stay remote. You hate remote work and need to be local.",
        False,
        tags=("line_to_line", "fail"),
    ),
    ConsistencyScenario(
        "qa_pass_still_burned",
        "am I still burned out on side projects?",
        _bundle("Recent chat: side projects spike when I'm avoiding rest."),
        (
            ("user", "was I burned out last month?"),
            ("assistant", "Yes, you were still burned out and skipping sleep for side projects."),
        ),
        "Yes, you're still burned out — same pattern as last month.",
        True,
        tags=("qa", "pass"),
    ),
    ConsistencyScenario(
        "qa_fail_burned_flip",
        "burnout check — be honest",
        _bundle(),
        (
            ("user", "was I burned out?"),
            ("assistant", "You were burned out and needed rest, not another infra sprint."),
        ),
        "No, you were never burned out — you love grinding on infra every night.",
        False,
        tags=("qa", "fail"),
    ),
    ConsistencyScenario(
        "combo_pass_bundle_and_history",
        "does my memory say I still choose local life?",
        _bundle("2024 journal: I choose local life when family stuff is heavy."),
        (
            ("user", "local vs remote again"),
            ("assistant", "You prefer local when family obligations spike."),
        ),
        "Memory matches: you choose local life when family is heavy, same as before.",
        True,
        tags=("combo", "pass"),
    ),
    ConsistencyScenario(
        "combo_fail_bundle_and_l2l",
        "minimal tools again — straight answer",
        _bundle("I prefer minimal tools when I'm tired of infra."),
        (),
        "You prefer minimal tools when infra fatigue hits. You hate minimal tools and want complexity.",
        False,
        tags=("combo", "fail"),
    ),
    ConsistencyScenario(
        "ptl_pass_constraints",
        "what's the honesty rule again?",
        _bundle(constraints=("No fabricated memories — say I don't remember.",)),
        (),
        "Honesty anchor: no fabricated memories; say you don't remember when it's missing.",
        True,
        tags=("prompt_to_line", "constraints"),
    ),
    ConsistencyScenario(
        "ptl_fail_remote_stance",
        "remote work — what's my actual stance?",
        _bundle("2025 note: I prefer remote when deep work matters."),
        (),
        "You hate remote and need to be local full time.",
        False,
        tags=("prompt_to_line", "fail"),
    ),
    ConsistencyScenario(
        "l2l_pass_single_stance",
        "one line check",
        _bundle(),
        (),
        "You are still kind when you're not performing Analyst mode.",
        True,
        tags=("line_to_line",),
    ),
    ConsistencyScenario(
        "qa_pass_no_prior_assistant",
        "fresh thread — do I like simple tools?",
        _bundle("I prefer simple tools for solo apps."),
        (("user", "hey"),),
        "You prefer simple tools for solo apps — that's what your notes say.",
        True,
        tags=("qa", "no_assistant_history"),
    ),
)


def load_consistency_scenarios() -> list[ConsistencyScenario]:
    return list(CONSISTENCY_SCENARIOS)
