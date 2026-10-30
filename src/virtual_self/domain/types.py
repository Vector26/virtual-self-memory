from __future__ import annotations

from enum import StrEnum


class MemoryKind(StrEnum):
    PREFERENCE = "preference"
    GOAL = "goal"
    EPISODE = "episode"
    RELATIONSHIP = "relationship"
    IDENTITY = "identity"
    CURRENT_STATE = "current_state"


class MemoryStatus(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    FORGOTTEN = "forgotten"
    PENDING_DELETION = "pending_deletion"


class Trust(StrEnum):
    USER_ASSERTED = "user_asserted"
    USER_APPROVED = "user_approved"
    AGENT_INFERRED = "agent_inferred"
    IMPORTED = "imported"
