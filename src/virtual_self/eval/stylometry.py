from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from virtual_self.eval.notion_self_text import word_count

_MD_HEADER = re.compile(r"^#{1,6}\s", re.M)
_MD_BOLD = re.compile(r"\*\*")
_QUESTION_CLOSER = re.compile(
    r"(?i)(?:\?\s*$|does this match|hope this helps|let me know if|anything else\b)"
)
_SENTENCE_SPLIT = re.compile(r"[.!?]+\s+|\n+")


@dataclass(frozen=True)
class StylometryFeatures:
    words: int
    sentences: int
    words_per_message: float
    sentences_per_message: float
    markdown_markers_per_100_words: float
    question_closer_rate: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def stylometry_features(text: str) -> StylometryFeatures:
    stripped = (text or "").strip()
    words = word_count(stripped)
    if words == 0:
        return StylometryFeatures(0, 0, 0.0, 0.0, 0.0, 0.0)

    parts = [p for p in _SENTENCE_SPLIT.split(stripped) if p.strip()]
    sentences = max(1, len(parts))
    md_count = len(_MD_HEADER.findall(stripped)) + len(_MD_BOLD.findall(stripped))
    md_per_100 = (md_count / words) * 100.0
    closers = 1 if _QUESTION_CLOSER.search(stripped) else 0

    return StylometryFeatures(
        words=words,
        sentences=sentences,
        words_per_message=float(words),
        sentences_per_message=float(sentences),
        markdown_markers_per_100_words=round(md_per_100, 4),
        question_closer_rate=float(closers),
    )


def mean_features(features: list[StylometryFeatures]) -> dict[str, float]:
    if not features:
        return {}
    keys = ("words_per_message", "sentences_per_message", "markdown_markers_per_100_words", "question_closer_rate")
    out: dict[str, float] = {}
    for key in keys:
        vals = [getattr(f, key) for f in features]
        out[key] = round(sum(vals) / len(vals), 4)
    return out
