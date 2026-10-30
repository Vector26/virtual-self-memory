from __future__ import annotations

from virtual_self.enrichment.grounding import grounding_score, passes_grounding


def test_grounding_hits_source_terms():
    src = "# Home server\nDocker and Ansible for homelab automation."
    summ = "The user completed a home server quest using Docker and Ansible for homelab automation."
    assert grounding_score(summ, src, "Home server") >= 0.5


def test_grounding_rejects_fabricated_proper_nouns():
    src = "# Todo\nbuy milk"
    summ = "The user studied at Oxford and leads a fintech startup in London."
    assert not passes_grounding(summ, src, "Todo")
