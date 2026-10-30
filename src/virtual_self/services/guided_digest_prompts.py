"""Fixed guided questions for GPG-style chapter digests (TR-2 / plan §P4)."""

from __future__ import annotations

from typing import Literal

Layer = Literal["values", "voice", "decision_style", "modes"]

PROMPT_VERSION = "guided_chapter_digest_v1"

GUIDED_QUESTIONS: dict[Layer, tuple[tuple[str, str], ...]] = {
    "values": (
        ("authority_disagree", "When you disagree with authority, what do you write?"),
        ("self_praise", "What do you praise in yourself?"),
        ("non_negotiable", "What do you treat as non-negotiable?"),
    ),
    "voice": (
        ("exhausted", "How do you talk when exhausted?"),
        ("register_shift", "How does your tone shift between journal and casual chat?"),
    ),
    "decision_style": (
        ("uncertainty", "How do you decide under uncertainty?"),
        ("fast_decisions", "What do you avoid deciding too quickly?"),
    ),
    "modes": (
        ("integrated_self", "When you speak as your integrated self, what shows up?"),
        ("inner_voices", "Where do distinct inner voices or modes appear in your writing?"),
    ),
}

GUIDED_DIGEST_SYSTEM = """You synthesize a chapter-scoped guided digest from the user's own memories.
Rules:
- Use ONLY the provided memory snippets. Every answer and profile sentence MUST cite memory_ids from the bundle.
- Do not invent facts, diagnoses, or clinical labels.
- Label stance per item: actual (observed behavior), aspiration (goals/wants), mode_difference (contrasts between modes).
- Note journaling bias if low-mood journal entries dominate the bundle.
- Preserve contradictions as mode_difference; do not average them away.

Output JSON:
{
  "journaling_bias_note": "string or empty",
  "guided": [
    {"layer":"values|voice|decision_style|modes","question_key":"...","answer":"...","memory_ids":["uuid",...],"stance":"actual|aspiration|mode_difference"}
  ],
  "profile": [
    {"layer":"values|voice|decision_style|modes","sentence":"...","memory_ids":["uuid",...],"stance":"actual|aspiration|mode_difference"}
  ]
}"""
