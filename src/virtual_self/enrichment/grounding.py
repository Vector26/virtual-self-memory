from __future__ import annotations

import re

_WORD = re.compile(r"[a-z0-9]{4,}", re.I)

# If the model leans on these without source support, treat as low grounding.
_SPECULATION = frozenset(
    {
        "probably",
        "likely",
        "maybe",
        "perhaps",
        "seems",
        "appears",
        "must",
        "clearly",
        "obviously",
        "always",
        "never",
        "definitely",
    }
)


def _tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _WORD.finditer(text)}


def grounding_score(summary: str, source_plain: str, title: str | None = None) -> float:
    """Fraction of summary content-words that appear in title or source (0-1)."""
    summ = _tokens(summary)
    if not summ:
        return 0.0
    corpus = _tokens(source_plain)
    if title:
        corpus |= _tokens(title)
    if not corpus:
        return 0.0
    hit = sum(1 for t in summ if t in corpus)
    return hit / len(summ)


def adjust_confidence(summary: str, model_confidence: float, score: float) -> float:
    """Pull confidence down when grounding is weak or summary is speculative."""
    conf = max(0.0, min(1.0, model_confidence))
    words = summary.lower().split()
    spec = sum(1 for w in words if w.strip(".,;:") in _SPECULATION)
    if spec >= 2:
        conf = min(conf, 0.55)
    if score < 0.35:
        return min(conf, 0.45)
    if score < 0.5:
        return min(conf, 0.65)
    if score < 0.65:
        return min(conf, 0.8)
    return conf


def passes_grounding(
    summary: str,
    source_plain: str,
    title: str | None,
    *,
    min_score: float = 0.35,
) -> bool:
    return grounding_score(summary, source_plain, title) >= min_score
