from __future__ import annotations

import re

_MAX_VARIANTS = 4

_BOILERPLATE_PREFIX = re.compile(
    r"^\s*(?:who\s+(?:was|is)|tell\s+me\s+about)\s+",
    re.IGNORECASE,
)
_QUOTED = re.compile(r'"([^"]+)"|\'([^\']+)\'')
_NAME_RUN = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b")
_CAP_STOP = frozenset(
    {
        "a",
        "an",
        "about",
        "can",
        "do",
        "how",
        "i",
        "is",
        "me",
        "tell",
        "the",
        "was",
        "what",
        "when",
        "where",
        "who",
        "why",
        "you",
    }
)


def _strip_boilerplate(text: str) -> str:
    t = text.strip()
    while True:
        nxt = _BOILERPLATE_PREFIX.sub("", t).strip()
        if nxt == t:
            break
        t = nxt
    return t.rstrip("?").strip()


def _quoted_strings(text: str) -> list[str]:
    out: list[str] = []
    for m in _QUOTED.finditer(text):
        chunk = m.group(1) if m.group(1) is not None else m.group(2)
        if chunk and chunk.strip():
            out.append(chunk.strip())
    return out


def _boilerplate_name_variants(stripped: str) -> list[str]:
    """Title-case and token variants from text after who is / who was / tell me about."""
    core = stripped.rstrip("?").strip()
    if not core:
        return []
    words = core.split()
    if not words:
        return []
    titled = " ".join(w.capitalize() for w in words)
    out: list[str] = [core] if core != titled else []
    if titled not in out:
        out.append(titled)
    token_words = words[1:] if len(words) > 1 else words
    for part in token_words:
        tok = part.strip("?.,!'\"")
        if len(tok) >= 3 and tok.lower() not in _CAP_STOP:
            out.append(tok)
    return out


def _capitalized_name_phrases(text: str) -> list[str]:
    out: list[str] = []
    for m in _NAME_RUN.finditer(text):
        phrase = m.group(0).strip()
        words = phrase.split()
        if not words:
            continue
        if len(words) == 1 and words[0].lower() in _CAP_STOP:
            continue
        if all(w.lower() in _CAP_STOP for w in words):
            continue
        out.append(phrase)
    return out


def queries_for_retrieval(user_text: str) -> list[str]:
    """Build up to four deduplicated search queries from chat user text."""
    trimmed = user_text.strip()
    if not trimmed:
        return []

    seen: set[str] = set()
    variants: list[str] = []

    def add(candidate: str) -> bool:
        q = candidate.strip()
        if not q:
            return False
        if q in seen:
            return False
        seen.add(q)
        variants.append(q)
        return len(variants) >= _MAX_VARIANTS

    if add(trimmed):
        return variants

    for quoted in _quoted_strings(trimmed):
        if add(quoted):
            return variants

    stripped = _strip_boilerplate(trimmed)
    core_trimmed = trimmed.rstrip("?").strip()
    boilerplate_stripped = stripped.casefold() != core_trimmed.casefold()

    if boilerplate_stripped and stripped:
        for candidate in _boilerplate_name_variants(stripped):
            if add(candidate):
                return variants
    else:
        for name in _capitalized_name_phrases(trimmed):
            if add(name):
                return variants

    return variants[:_MAX_VARIANTS]
