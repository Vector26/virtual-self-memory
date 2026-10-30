from __future__ import annotations

import json
from pathlib import Path

from virtual_self.auth.credentials import resolve_auth
from virtual_self.config import get_settings
from virtual_self.eval.llm_likeness import CharNgramLlmClassifier, seed_llm_training_texts
from virtual_self.services.llm_likeness_pass import EXCLUDED_REASON, run_llm_likeness_pass

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TRAIN = FIXTURES / "llm_likeness_train.json"
LABELED = FIXTURES / "llm_likeness_labeled.json"


def _load_train() -> tuple[list[str], list[str]]:
    data = json.loads(TRAIN.read_text(encoding="utf-8"))
    return list(data["self"]), list(data["llm"])


def _load_labeled() -> list[tuple[str, str]]:
    rows = json.loads(LABELED.read_text(encoding="utf-8"))
    return [(r["text"], r["label"]) for r in rows]


def test_char_ngram_classifier_accuracy_on_labeled_sample():
    self_texts, llm_texts = _load_train()
    labeled = _load_labeled()
    assert len(labeled) == 40

    clf = CharNgramLlmClassifier().fit(self_texts, llm_texts)
    clf.calibrate_threshold(labeled)
    acc = clf.accuracy(labeled)
    assert acc >= 0.90, f"accuracy {acc:.3f} below 0.90 (threshold={clf.threshold})"


def test_seed_llm_corpus_non_empty():
    assert len(seed_llm_training_texts()) >= 5


def test_run_llm_likeness_pass_dry_run(ensure_db):
    get_settings.cache_clear()
    token = get_settings().bootstrap_token
    auth = resolve_auth(f"Bearer {token.get_secret_value()}")
    out = run_llm_likeness_pass(auth, dry_run=True)
    assert out["dry_run"] is True
    assert "stats" in out
    assert "review_doc" in out
    assert EXCLUDED_REASON in out["review_doc"] or "none above threshold" in out["review_doc"]
