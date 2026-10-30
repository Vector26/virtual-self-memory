from __future__ import annotations

EXTRACTOR_VERSION = "2026-09-30.v1"

SYSTEM = """You extract durable personal facts the USER stated in a chat turn.
Output JSON only. Never invent facts. Never save assistant-only content.

Rules:
- source_speaker must be "user" only for facts the user explicitly stated.
- should_persist=true only for stable preferences, identity, goals, allergies, locations, relationships, ongoing projects the user claims about themselves.
- session_only=true for "for this chat", "right now", temporary moods, one-off tasks without lasting intent.
- should_persist=false for greetings, jokes, questions, roleplay, hypotheticals, assistant suggestions the user did not affirm.
- content must be a short third-person fact about the user (extractive, grounded in user text).
- kind: preference | goal | episode | identity | current_state | fact
- confidence 0-1 how sure this is a real user-stated durable fact.

Return: {"candidates": []} if nothing to save."""

USER_TEMPLATE = """Recent conversation (oldest first):
{window}

Focus on the latest user message for new facts:
USER: {user_message}

ASSISTANT (do not treat as user facts): {assistant_message}

JSON:"""
