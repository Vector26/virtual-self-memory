"""Register word-count norms for governor length checks (persona plan §P7.1)."""

from __future__ import annotations

REGISTER_WORD_NORMS: dict[str, int] = {
    "casual_chat": 80,
    "journal": 140,
    "technical": 180,
    "grief": 160,
    "angry": 100,
    "playful": 90,
    "decision": 120,
}

DEFAULT_REGISTER = "casual_chat"


def register_word_norm(register: str) -> int:
    return REGISTER_WORD_NORMS.get(register, REGISTER_WORD_NORMS[DEFAULT_REGISTER])
