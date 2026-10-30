from __future__ import annotations

from virtual_self.enrichment.prompts import PAGE_ENRICHMENT_SYSTEM, PERSONALITY_SYNTHESIS_SYSTEM


def test_page_prompt_covers_corpus_safeguards():
    assert "aspirational" in PAGE_ENRICHMENT_SYSTEM
    assert "Status" in PAGE_ENRICHMENT_SYSTEM
    assert "EXTRACTIVE" in PAGE_ENRICHMENT_SYSTEM
    assert "OMIT" in PAGE_ENRICHMENT_SYSTEM
    assert "memory_kind" in PAGE_ENRICHMENT_SYSTEM


def test_personality_prompt_covers_facets():
    for key in (
        "constitution",
        "values",
        "voice",
        "interaction_preferences",
        "current_priorities",
        "decision_style",
    ):
        assert key in PERSONALITY_SYNTHESIS_SYSTEM
