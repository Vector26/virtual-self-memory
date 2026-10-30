"""Char n-gram classifier: user self text vs generic LLM prose (CPU-only)."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from collections.abc import Iterable

from virtual_self.eval.notion_self_text import word_count

_WS = re.compile(r"\s+")
DEFAULT_N_MIN = 3
DEFAULT_N_MAX = 5
DEFAULT_ALPHA = 0.5
MIN_TRAIN_WORDS = 12
MIN_SCORE_WORDS = 8

# Synthetic LLM-like prose when DB has few author=llm rows (A-3 structural splits).
_SEED_LLM_CORPUS: tuple[str, ...] = (
    (
        "Certainly! Here is a comprehensive overview of your question. "
        "Below are five key points to consider as you move forward on this journey."
    ),
    (
        "As an AI language model, I cannot provide medical advice, but I want to acknowledge "
        "that your feelings are valid. It's okay to feel overwhelmed during periods of change."
    ),
    (
        "Great question! Let's break this down step by step. First, we'll explore the context. "
        "Second, we'll examine practical strategies. Does this match your experience?"
    ),
    (
        "In conclusion, implementing these best practices will help you achieve meaningful growth. "
        "Feel free to ask if you'd like me to elaborate on any section."
    ),
    (
        "Your situation reveals a profound existential tapestry woven with grief, identity, "
        "and a search for meaning. Below I outline a structured framework for reflection."
    ),
    (
        "## Summary\n\n| Topic | Insight |\n| --- | --- |\n| Mindset | Stay curious |\n\n"
        "I hope this helps you navigate the path ahead."
    ),
    (
        "Absolutely! Here are several actionable recommendations:\n\n1. Start with clarity\n"
        "2. Build consistent habits\n3. Reflect weekly\n\nLet me know if you need anything else."
    ),
)


def normalize_for_ngrams(text: str) -> str:
    return _WS.sub(" ", (text or "").strip().lower())


def char_ngram_counts(text: str, *, n_min: int = DEFAULT_N_MIN, n_max: int = DEFAULT_N_MAX) -> Counter[str]:
    norm = normalize_for_ngrams(text)
    counts: Counter[str] = Counter()
    if len(norm) < n_min:
        return counts
    for n in range(n_min, n_max + 1):
        for i in range(len(norm) - n + 1):
            counts[norm[i : i + n]] += 1
    return counts


def _log_prob(count: int, total: int, vocab: int, alpha: float) -> float:
    return math.log(count + alpha) - math.log(total + alpha * max(vocab, 1))


@dataclass
class CharNgramLlmClassifier:
    """Higher score ⇒ more LLM-like (log-odds: P(llm|text) vs P(self|text))."""

    n_min: int = DEFAULT_N_MIN
    n_max: int = DEFAULT_N_MAX
    alpha: float = DEFAULT_ALPHA
    threshold: float = 0.0
    self_counts: Counter[str] = field(default_factory=Counter)
    llm_counts: Counter[str] = field(default_factory=Counter)
    self_total: int = 0
    llm_total: int = 0
    vocab_size: int = 0

    def fit(self, self_texts: Iterable[str], llm_texts: Iterable[str]) -> CharNgramLlmClassifier:
        self.self_counts = Counter()
        self.llm_counts = Counter()
        self.self_total = 0
        self.llm_total = 0

        for raw in self_texts:
            if word_count(raw) < MIN_TRAIN_WORDS:
                continue
            grams = char_ngram_counts(raw, n_min=self.n_min, n_max=self.n_max)
            self.self_counts.update(grams)
            self.self_total += sum(grams.values())

        for raw in llm_texts:
            if word_count(raw) < MIN_TRAIN_WORDS:
                continue
            grams = char_ngram_counts(raw, n_min=self.n_min, n_max=self.n_max)
            self.llm_counts.update(grams)
            self.llm_total += sum(grams.values())

        vocab = set(self.self_counts) | set(self.llm_counts)
        self.vocab_size = max(len(vocab), 1)
        return self

    def score(self, text: str) -> float:
        if word_count(text) < MIN_SCORE_WORDS:
            return float("-inf")
        grams = char_ngram_counts(text, n_min=self.n_min, n_max=self.n_max)
        if not grams:
            return float("-inf")

        llm_ll = 0.0
        self_ll = 0.0
        for gram, cnt in grams.items():
            llm_ll += cnt * _log_prob(
                self.llm_counts.get(gram, 0), self.llm_total, self.vocab_size, self.alpha
            )
            self_ll += cnt * _log_prob(
                self.counts_self(gram), self.self_total, self.vocab_size, self.alpha
            )
        return llm_ll - self_ll

    def counts_self(self, gram: str) -> int:
        return self.self_counts.get(gram, 0)

    def predict_llm(self, text: str) -> bool:
        return self.score(text) >= self.threshold

    def calibrate_threshold(self, labeled: Iterable[tuple[str, str]]) -> float:
        """Pick threshold maximizing accuracy on (text, 'self'|'llm') labels."""
        pairs = [(t, y) for t, y in labeled if y in ("self", "llm") and word_count(t) >= MIN_SCORE_WORDS]
        if not pairs:
            self.threshold = 0.0
            return self.threshold

        scores = [self.score(text) for text, _ in pairs]
        candidates = sorted(set(scores))
        if not candidates:
            self.threshold = 0.0
            return self.threshold

        best_t = candidates[0]
        best_acc = -1.0
        for mid in candidates:
            correct = sum(
                1 for (text, label), sc in zip(pairs, scores, strict=True)
                if (sc >= mid and label == "llm") or (sc < mid and label == "self")
            )
            acc = correct / len(pairs)
            if acc > best_acc:
                best_acc = acc
                best_t = mid
        self.threshold = best_t
        return self.threshold

    def accuracy(self, labeled: Iterable[tuple[str, str]]) -> float:
        pairs = [(t, y) for t, y in labeled if y in ("self", "llm")]
        if not pairs:
            return 0.0
        correct = sum(
            1
            for text, label in pairs
            if (self.predict_llm(text) and label == "llm") or (not self.predict_llm(text) and label == "self")
        )
        return correct / len(pairs)


def seed_llm_training_texts() -> list[str]:
    return list(_SEED_LLM_CORPUS)
