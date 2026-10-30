from __future__ import annotations

# Only this producer may write summaries/labels/kinds for import and personality synthesis.
CURSOR_AGENT_PRODUCER = "cursor_agent"
PROMPT_VERSION = "2026-09-29.v3-grounded"

ALLOWED_MEMORY_KINDS = frozenset(
    {
        "goal",
        "skill",
        "preference",
        "episode",
        "routine",
        "identity",
        "current_state",
    }
)
