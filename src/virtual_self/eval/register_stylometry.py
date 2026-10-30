from __future__ import annotations

import math
import re
import statistics
from collections import Counter
from dataclasses import dataclass
from typing import Any

from virtual_self.eval.notion_self_text import word_count
from virtual_self.eval.stylometry import stylometry_features

MIN_REGISTER_WORDS = 2000
PRIMARY_REGISTERS = ("casual_chat", "journal", "technical")

_EMOJI = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U0001F600-\U0001F64F"
    "]",
    flags=re.UNICODE,
)
_TOKEN = re.compile(r"[a-z']+", re.I)
_SENTENCE_END = re.compile(r"[.!?]+\s*")
_ELLIPSIS = re.compile(r"\.{3,}|…")
_ALL_CAPS = re.compile(r"\b[A-Z]{2,}\b")
_BULLET = re.compile(r"^(?:[-*•]|\d+\.)\s", re.M)
_MD_HEADER = re.compile(r"^#{1,6}\s", re.M)
_MD_BOLD = re.compile(r"\*\*")
_QUESTION_CLOSER = re.compile(
    r"(?i)(?:\?\s*$|does this match|hope this helps|let me know if|anything else\b)"
)

_HEDGES = frozenset(
    {
        "maybe",
        "perhaps",
        "probably",
        "kind of",
        "sort of",
        "i think",
        "i guess",
        "might",
        "could",
        "somewhat",
        "basically",
    }
)
_INTENSIFIERS = frozenset({"very", "really", "extremely", "absolutely", "literally", "so", "totally"})
_FILLERS = frozenset({"like", "um", "uh", "you know", "i mean"})
_PROFANITY = frozenset({"fuck", "fucking", "shit", "damn", "hell", "ass", "bitch", "crap"})

_FUNCTION_WORDS = (
    "the",
    "of",
    "and",
    "to",
    "a",
    "in",
    "that",
    "it",
    "is",
    "was",
    "for",
    "on",
    "as",
    "with",
    "his",
    "he",
    "be",
    "at",
    "by",
    "i",
    "this",
    "had",
    "not",
    "but",
    "from",
    "they",
    "we",
    "say",
    "her",
    "she",
    "or",
    "an",
    "will",
    "my",
    "one",
    "all",
    "would",
    "there",
    "their",
    "what",
    "so",
    "up",
    "out",
    "if",
    "about",
    "who",
    "get",
    "which",
    "go",
    "me",
    "when",
    "make",
    "can",
    "like",
    "time",
    "no",
    "just",
    "him",
    "know",
    "take",
    "people",
    "into",
    "year",
    "your",
    "good",
    "some",
    "could",
    "them",
    "see",
    "other",
    "than",
    "then",
    "now",
    "look",
    "only",
    "come",
    "its",
    "over",
    "think",
    "also",
    "back",
    "after",
    "use",
    "two",
    "how",
    "our",
    "work",
    "first",
    "well",
    "way",
    "even",
    "new",
    "want",
    "because",
    "any",
    "these",
    "give",
    "day",
    "most",
    "us",
)

# Inverse-rank proxy for general English (Burrows / distinctiveness baseline).
_ENGLISH_BASELINE: dict[str, float] = {
    "the": 1000.0,
    "be": 900.0,
    "to": 880.0,
    "of": 860.0,
    "and": 840.0,
    "a": 820.0,
    "in": 800.0,
    "that": 780.0,
    "have": 760.0,
    "i": 740.0,
    "it": 720.0,
    "for": 700.0,
    "not": 680.0,
    "on": 660.0,
    "with": 640.0,
    "he": 620.0,
    "as": 600.0,
    "you": 580.0,
    "do": 560.0,
    "at": 540.0,
}

_STOP = frozenset(_FUNCTION_WORDS) | frozenset(
    {"is", "was", "are", "were", "been", "being", "am", "has", "had", "have", "does", "did"}
)


def _dist_stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "p50": 0.0, "p90": 0.0, "min": 0.0, "max": 0.0}
    ordered = sorted(values)
    n = len(ordered)

    def pct(p: float) -> float:
        if n == 1:
            return ordered[0]
        idx = min(n - 1, max(0, int(math.ceil(p * n)) - 1))
        return ordered[idx]

    return {
        "mean": round(statistics.fmean(values), 4),
        "p50": round(pct(0.5), 4),
        "p90": round(pct(0.9), 4),
        "min": round(ordered[0], 4),
        "max": round(ordered[-1], 4),
    }


def _rate_per_100_words(count: int, words: int) -> float:
    if words <= 0:
        return 0.0
    return round((count / words) * 100.0, 4)


def _phrase_hits(text_lower: str, phrases: frozenset[str]) -> int:
    hits = 0
    for phrase in phrases:
        if " " in phrase:
            hits += text_lower.count(phrase)
        else:
            hits += len(re.findall(rf"\b{re.escape(phrase)}\b", text_lower))
    return hits


def _lowercase_sentence_start_rate(text: str) -> float:
    if not text.strip():
        return 0.0
    starts = 0
    lower_starts = 0
    for chunk in re.split(r"(?<=[.!?])\s+|\n+", text.strip()):
        chunk = chunk.lstrip()
        if not chunk:
            continue
        starts += 1
        if chunk[0].isalpha() and chunk[0].islower():
            lower_starts += 1
    if starts == 0:
        return 0.0
    return round(lower_starts / starts, 4)


def _lowercase_i_rate(text: str) -> float:
    i_lower = len(re.findall(r"\bi\b", text))
    i_upper = len(re.findall(r"\bI\b", text))
    total = i_lower + i_upper
    if total == 0:
        return 0.0
    return round(i_lower / total, 4)


def _missing_terminal_punctuation_rate(text: str) -> float:
    stripped = text.strip()
    if not stripped:
        return 0.0
    chunks = [c.strip() for c in re.split(r"\n+", stripped) if c.strip()]
    if not chunks:
        return 0.0
    missing = sum(1 for c in chunks if not re.search(r"[.!?…]$", c))
    return round(missing / len(chunks), 4)


def _distinctive_lexicon(word_counts: Counter[str], n_words: int, *, limit: int = 20) -> list[dict[str, Any]]:
    scored: list[tuple[float, str, int]] = []
    for word, count in word_counts.items():
        if count < 3 or word in _STOP or len(word) < 3:
            continue
        baseline = _ENGLISH_BASELINE.get(word, 8.0)
        per_k = (count / max(n_words, 1)) * 1000.0
        score = per_k / baseline
        scored.append((score, word, count))
    scored.sort(reverse=True)
    out: list[dict[str, Any]] = []
    for score, word, count in scored[:limit]:
        out.append(
            {
                "word": word,
                "count": count,
                "per_1000_words": round((count / max(n_words, 1)) * 1000.0, 4),
                "distinctiveness_score": round(score, 4),
            }
        )
    return out


def _top_pet_phrases(texts: list[str], *, limit: int = 5) -> list[dict[str, Any]]:
    bigrams: Counter[str] = Counter()
    for text in texts:
        words = [w.lower() for w in _TOKEN.findall(text)]
        for i in range(len(words) - 1):
            bg = f"{words[i]} {words[i + 1]}"
            if words[i] in _STOP and words[i + 1] in _STOP:
                continue
            bigrams[bg] += 1
    return [{"phrase": p, "count": c} for p, c in bigrams.most_common(limit) if c >= 3]


def _function_word_vector(word_counts: Counter[str], n_words: int) -> dict[str, float]:
    if n_words <= 0:
        return {}
    out: dict[str, float] = {}
    for fw in _FUNCTION_WORDS:
        out[fw] = round(word_counts.get(fw, 0) / n_words, 6)
    return out


@dataclass(frozen=True)
class TextStylometrySlice:
    words: int
    sentences: int
    words_per_sentence: float
    question_closer: float


def extract_text_slice(text: str) -> TextStylometrySlice:
    base = stylometry_features(text)
    wps = round(base.words / max(base.sentences, 1), 4)
    return TextStylometrySlice(
        words=base.words,
        sentences=base.sentences,
        words_per_sentence=wps,
        question_closer=base.question_closer_rate,
    )


def extract_text_features(text: str) -> dict[str, Any]:
    stripped = (text or "").strip()
    words = word_count(stripped)
    if words == 0:
        return {"words": 0}

    slice_ = extract_text_slice(stripped)
    lower = stripped.lower()
    tokens = [t.lower() for t in _TOKEN.findall(stripped)]
    token_counts = Counter(tokens)

    sentences = max(1, slice_.sentences)
    question_sentences = len(re.findall(r"\?\s*(?:\n|$)", stripped)) + (
        1 if stripped.rstrip().endswith("?") else 0
    )

    return {
        "words": words,
        "sentences": slice_.sentences,
        "words_per_message": float(words),
        "sentences_per_message": float(slice_.sentences),
        "words_per_sentence": slice_.words_per_sentence,
        "question_rate": round(min(1.0, question_sentences / sentences), 4),
        "question_closer_rate": slice_.question_closer,
        "markdown_headers_per_100_words": _rate_per_100_words(len(_MD_HEADER.findall(stripped)), words),
        "markdown_bold_per_100_words": _rate_per_100_words(len(_MD_BOLD.findall(stripped)), words),
        "markdown_bullets_per_100_words": _rate_per_100_words(len(_BULLET.findall(stripped)), words),
        "periods_per_100_words": _rate_per_100_words(stripped.count("."), words),
        "ellipses_per_100_words": _rate_per_100_words(len(_ELLIPSIS.findall(stripped)), words),
        "exclamations_per_100_words": _rate_per_100_words(stripped.count("!"), words),
        "questions_per_100_words": _rate_per_100_words(stripped.count("?"), words),
        "commas_per_100_words": _rate_per_100_words(stripped.count(","), words),
        "emoji_per_100_words": _rate_per_100_words(len(_EMOJI.findall(stripped)), words),
        "hedges_per_100_words": _rate_per_100_words(_phrase_hits(lower, _HEDGES), words),
        "intensifiers_per_100_words": _rate_per_100_words(_phrase_hits(lower, _INTENSIFIERS), words),
        "fillers_per_100_words": _rate_per_100_words(_phrase_hits(lower, _FILLERS), words),
        "profanity_per_100_words": _rate_per_100_words(_phrase_hits(lower, _PROFANITY), words),
        "lowercase_sentence_start_rate": _lowercase_sentence_start_rate(stripped),
        "lowercase_i_rate": _lowercase_i_rate(stripped),
        "all_caps_emphasis_per_100_words": _rate_per_100_words(len(_ALL_CAPS.findall(stripped)), words),
        "missing_terminal_punctuation_rate": _missing_terminal_punctuation_rate(stripped),
        "_tokens": tokens,
    }


def aggregate_register_profile(texts: list[str]) -> dict[str, Any]:
    per_text = [extract_text_features(t) for t in texts if (t or "").strip()]
    n_texts = len(per_text)
    n_words = sum(p["words"] for p in per_text)

    sufficient = n_words >= MIN_REGISTER_WORDS
    insufficient_reason = None
    if not sufficient:
        insufficient_reason = f"only {n_words} train words (need >={MIN_REGISTER_WORDS})"

    if n_texts == 0:
        return {
            "schema_version": 1,
            "sufficient": False,
            "insufficient_reason": insufficient_reason or "no texts",
            "n_texts": 0,
            "n_words": 0,
            "length": {},
            "punctuation_per_100_words": {},
            "markdown_per_100_words": {},
            "lexicon": {},
            "function_word_frequencies": {},
            "question_rate": 0.0,
            "question_closer_rate": 0.0,
            "orthographic": {},
            "orthographic_not_for_imitation": True,
        }

    token_counts: Counter[str] = Counter()
    for p in per_text:
        token_counts.update(p.pop("_tokens", []))

    length_keys = ("words_per_message", "sentences_per_message", "words_per_sentence")
    length = {k: _dist_stats([float(p[k]) for p in per_text]) for k in length_keys}

    punct_keys = (
        "periods_per_100_words",
        "ellipses_per_100_words",
        "exclamations_per_100_words",
        "questions_per_100_words",
        "commas_per_100_words",
    )
    md_keys = (
        "markdown_headers_per_100_words",
        "markdown_bold_per_100_words",
        "markdown_bullets_per_100_words",
    )
    ortho_keys = (
        "lowercase_sentence_start_rate",
        "lowercase_i_rate",
        "all_caps_emphasis_per_100_words",
        "missing_terminal_punctuation_rate",
    )

    return {
        "schema_version": 1,
        "sufficient": sufficient,
        "insufficient_reason": insufficient_reason,
        "n_texts": n_texts,
        "n_words": n_words,
        "length": length,
        "punctuation_per_100_words": {
            k: round(statistics.fmean([p[k] for p in per_text]), 4) for k in punct_keys
        },
        "markdown_per_100_words": {k: round(statistics.fmean([p[k] for p in per_text]), 4) for k in md_keys},
        "lexicon": {
            "distinctive_words": _distinctive_lexicon(token_counts, n_words),
            "pet_phrases": _top_pet_phrases(texts),
            "hedges_per_100_words": round(statistics.fmean([p["hedges_per_100_words"] for p in per_text]), 4),
            "intensifiers_per_100_words": round(
                statistics.fmean([p["intensifiers_per_100_words"] for p in per_text]), 4
            ),
            "fillers_per_100_words": round(statistics.fmean([p["fillers_per_100_words"] for p in per_text]), 4),
            "profanity_per_100_words": round(statistics.fmean([p["profanity_per_100_words"] for p in per_text]), 4),
            "emoji_per_100_words": round(statistics.fmean([p["emoji_per_100_words"] for p in per_text]), 4),
        },
        "function_word_frequencies": _function_word_vector(token_counts, n_words),
        "question_rate": round(statistics.fmean([p["question_rate"] for p in per_text]), 4),
        "question_closer_rate": round(statistics.fmean([p["question_closer_rate"] for p in per_text]), 4),
        "orthographic": {k: round(statistics.fmean([p[k] for p in per_text]), 4) for k in ortho_keys},
        "orthographic_not_for_imitation": True,
    }
