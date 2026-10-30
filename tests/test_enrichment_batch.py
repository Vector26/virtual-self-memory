from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from virtual_self.auth.credentials import resolve_auth
from virtual_self.services.enrichment_batch import enrich_corpus

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "my_life_mini"


def test_enrich_corpus_mocked(ensure_db):
    auth = resolve_auth("Bearer test-bootstrap-token")
    fake = {
        "summary": "This is a long enough summary for validation rules on the mini fixture page.",
        "memory_kind": "goal",
        "labels": ["test"],
        "confidence": 0.9,
    }

    with patch("virtual_self.services.enrichment_batch.ensure_chat_model", return_value="test-model"):
        with patch("virtual_self.services.enrichment_batch.preflight_llm", return_value="mock"):
            with patch("virtual_self.services.enrichment_batch.notify_user"):
                with patch(
                    "virtual_self.services.enrichment_batch.enrich_page_with_agent",
                    return_value=fake,
                ):
                    out = enrich_corpus(auth.subject_id, FIXTURE_ROOT, only_missing=False)

    assert out["enriched"] >= 1
    assert out["failed"] == 0
