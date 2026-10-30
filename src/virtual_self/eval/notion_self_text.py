from __future__ import annotations

import re
from typing import Iterable

from virtual_self.ingestion.frontmatter import split_frontmatter

_LLM_ANSWER_SUMMARIES = re.compile(
    r"<details>\s*<summary>\s*###\s*(DS|GPT|Claude)\s*</summary>",
    re.IGNORECASE,
)
_LLM_ANSWER_BLOCK = re.compile(
    r"<details>\s*<summary>\s*###\s*(DS|GPT|Claude)\s*</summary>\s*(.*?)\s*</details>",
    re.IGNORECASE | re.DOTALL,
)
_LLM_MARKER = re.compile(r"###\s*(DS|GPT|Claude)\b", re.IGNORECASE)
_LONG_INLINE_QUOTE = re.compile(r'"([^"]{80,})"')
_MIN_QUOTED_WORDS = 8
_INITIAL_PROMPT = re.compile(
    r"<details>\s*<summary>\s*###\s*Initial\s+Prompt\s*</summary>\s*(.*?)\s*</details>",
    re.IGNORECASE | re.DOTALL,
)
_ANY_DETAILS = re.compile(r"<details>.*?</details>", re.IGNORECASE | re.DOTALL)
_BLOCKQUOTE = re.compile(r"^>\s.*$", re.MULTILINE)
_HEADING = re.compile(r"^#+\s+.*$", re.MULTILINE)
_WS = re.compile(r"\s+")


def word_count(text: str) -> int:
    return len(text.split())


def normalize_for_dedupe(text: str) -> str:
    return _WS.sub(" ", text.strip().lower())


def jaccard_words(a: str, b: str) -> float:
    sa = set(normalize_for_dedupe(a).split())
    sb = set(normalize_for_dedupe(b).split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def extract_llm_answer_blocks(body: str) -> list[tuple[str, str]]:
    """Return (model_tag, prose) for each LLM answer details block in page body."""
    out: list[tuple[str, str]] = []
    for match in _LLM_ANSWER_BLOCK.finditer(body):
        tag = match.group(1).upper()
        text = _WS.sub(" ", match.group(2).strip())
        if text:
            out.append((tag, text))
    return out


def peel_blockquotes(body: str) -> tuple[list[str], str]:
    """Split blockquote lines into quote chunks; return quotes and remaining body."""
    quotes: list[str] = []
    remaining: list[str] = []
    current: list[str] = []
    for line in body.splitlines():
        if line.startswith(">"):
            current.append(line[1:].lstrip())
            continue
        if current:
            chunk = "\n".join(current).strip()
            if chunk:
                quotes.append(chunk)
            current = []
        remaining.append(line)
    if current:
        chunk = "\n".join(current).strip()
        if chunk:
            quotes.append(chunk)
    return quotes, "\n".join(remaining)


def peel_long_inline_quotes(body: str) -> tuple[list[str], str]:
    quotes: list[str] = []
    for match in _LONG_INLINE_QUOTE.finditer(body):
        text = match.group(1).strip()
        if word_count(text) >= _MIN_QUOTED_WORDS:
            quotes.append(text)
    stripped = _LONG_INLINE_QUOTE.sub("", body)
    return quotes, stripped


def _strip_to_self_prose(body: str) -> str | None:
    stripped = _LLM_ANSWER_SUMMARIES.sub("", body)
    stripped = _ANY_DETAILS.sub("", stripped)
    stripped = _HEADING.sub("", stripped)
    stripped = _BLOCKQUOTE.sub("", stripped)
    text = _WS.sub(" ", stripped.strip())
    if not text or _LLM_MARKER.search(text):
        return None
    return text


def extract_self_text_from_markdown(raw: str) -> str | None:
    """Return user-authored prose from a My Life markdown page, or None if empty."""
    _meta, body = split_frontmatter(raw)
    prompts = [m.group(1).strip() for m in _INITIAL_PROMPT.finditer(body)]
    if prompts:
        text = "\n\n".join(prompts)
        text = _WS.sub(" ", text).strip()
    else:
        text = _strip_to_self_prose(body)
    if not text:
        return None
    if _LLM_MARKER.search(text):
        return None
    return text


def split_body_authorship(body: str) -> list[tuple[str, str, dict[str, str]]]:
    """
    Split page body into (author, content, meta) segments.

    author is one of: self, llm, quoted.
    meta may include llm_model for author=llm.
    """
    segments: list[tuple[str, str, dict[str, str]]] = []

    for model, llm_text in extract_llm_answer_blocks(body):
        segments.append(("llm", llm_text, {"llm_model": model}))

    prompts = [m.group(1).strip() for m in _INITIAL_PROMPT.finditer(body)]
    if prompts:
        for prompt in prompts:
            text = _WS.sub(" ", prompt).strip()
            if text and not _LLM_MARKER.search(text):
                segments.append(("self", text, {}))
        return segments

    blockquotes, remainder = peel_blockquotes(body)
    for quote in blockquotes:
        if word_count(quote) >= _MIN_QUOTED_WORDS:
            segments.append(("quoted", _WS.sub(" ", quote).strip(), {}))

    inline_quotes, remainder = peel_long_inline_quotes(remainder)
    for quote in inline_quotes:
        segments.append(("quoted", _WS.sub(" ", quote).strip(), {}))

    self_text = _strip_to_self_prose(remainder)
    if self_text:
        segments.append(("self", self_text, {}))

    return segments


def is_near_duplicate(text: str, existing: Iterable[str], threshold: float = 0.88) -> bool:
    norm = normalize_for_dedupe(text)
    for other in existing:
        if norm == normalize_for_dedupe(other):
            return True
        if len(norm) > 80 and (norm in normalize_for_dedupe(other) or normalize_for_dedupe(other) in norm):
            return True
        if jaccard_words(text, other) >= threshold:
            return True
    return False
