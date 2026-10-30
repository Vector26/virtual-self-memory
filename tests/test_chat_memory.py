from __future__ import annotations

from virtual_self.enrichment.grounding import grounding_score
from virtual_self.services.chat_memory_extract import parse_candidates


def test_parse_candidates_user_fact():
    raw = {
        "candidates": [
            {
                "content": "User is allergic to peanuts",
                "kind": "fact",
                "source_speaker": "user",
                "should_persist": True,
                "session_only": False,
                "confidence": 0.95,
            },
            {
                "content": "Assistant likes windshields",
                "kind": "fact",
                "source_speaker": "assistant",
                "should_persist": True,
                "session_only": False,
                "confidence": 0.99,
            },
        ]
    }
    items = parse_candidates(raw)
    assert len(items) == 2
    assert items[0]["source_speaker"] == "user"


def test_grounding_user_allergy():
    user = "I'm allergic to peanuts, please remember that."
    fact = "User is allergic to peanuts"
    assert grounding_score(fact, user, None) >= 0.25


def test_parse_empty_candidates():
    assert parse_candidates(None) == []
    assert parse_candidates({"candidates": "bad"}) == []
