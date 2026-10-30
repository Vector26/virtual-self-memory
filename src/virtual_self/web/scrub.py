from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import duckdb

from virtual_self.archive.paths import duckdb_path
from virtual_self.observability.logging import get_logger
from virtual_self.policy.sensitivity import SENSITIVITY_PRIVATE

log = get_logger(__name__)

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(
    r"(?<!\w)"
    r"(?:\+\d{1,3}[\s.-]?)?"
    r"(?:\(\d{2,4}\)[\s.-]?|\d{2,4}[\s.-]?)?"
    r"(?:\d[\s().-]*){7,}\d"
    r"(?!\d)"
)
ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9.'\s]{2,40}\s+"
    r"(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Ln|Lane|Dr|Drive|Way|Court|Ct)\b",
    re.I,
)
ACCOUNT_ID_RE = re.compile(r"\b(?:acct|account|iban|ssn)[:\s#-]*[A-Z0-9-]{8,}\b", re.I)
_LONG_ALNUM_RE = re.compile(r"\b[A-Z0-9]{18,}\b")

_NOTION_SEGMENT = re.compile(r"^(.+?)\s*\[[0-9a-f]{8}\]", re.I)
_TOKEN_RE = re.compile(r"[a-z0-9']+", re.I)

REDACT_EMAIL = "[EMAIL]"
REDACT_PHONE = "[PHONE]"
REDACT_ADDRESS = "[ADDRESS]"
REDACT_NAME = "[NAME]"
REDACT_PRIVATE = "[PRIVATE]"
REDACT_ACCOUNT = "[ACCOUNT]"

_MIN_NGRAM_WORDS = 5


@dataclass(frozen=True)
class ScrubResult:
    original: str
    scrubbed: str
    blocked: bool
    reasons: tuple[str, ...] = ()


@dataclass
class QueryScrubber:
    """Deterministic outbound search query scrubber (Plan §14.4)."""

    blocked_names: tuple[str, ...] = ()
    private_phrases: tuple[str, ...] = ()
    _name_patterns: list[re.Pattern[str]] = field(default_factory=list, repr=False)
    _phrase_patterns: list[re.Pattern[str]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        names = sorted({n.strip() for n in self.blocked_names if n and n.strip()}, key=len, reverse=True)
        object.__setattr__(self, "blocked_names", tuple(names))
        phrases = sorted(
            {_normalize_phrase(p) for p in self.private_phrases if p and len(p.split()) >= _MIN_NGRAM_WORDS},
            key=len,
            reverse=True,
        )
        object.__setattr__(self, "private_phrases", tuple(phrases))
        object.__setattr__(
            self,
            "_name_patterns",
            [_flex_phrase_pattern(n) for n in names if len(n.split()) >= 1],
        )
        object.__setattr__(
            self,
            "_phrase_patterns",
            [_flex_phrase_pattern(p) for p in phrases],
        )

    def scrub(self, query: str) -> ScrubResult:
        original = query.strip()
        if not original:
            return ScrubResult(original="", scrubbed="", blocked=True, reasons=("empty",))

        text = original
        reasons: list[str] = []

        text, n = _sub_all(EMAIL_RE, text, REDACT_EMAIL)
        if n:
            reasons.append("email")
        text, n = _sub_all(PHONE_RE, text, REDACT_PHONE)
        if n:
            reasons.append("phone")
        text, n = _sub_all(ADDRESS_RE, text, REDACT_ADDRESS)
        if n:
            reasons.append("address")
        text, n = _sub_all(ACCOUNT_ID_RE, text, REDACT_ACCOUNT)
        if n:
            reasons.append("account")
        text, n = _sub_all(_LONG_ALNUM_RE, text, REDACT_ACCOUNT)
        if n:
            reasons.append("long_id")

        for pat in self._phrase_patterns:
            text, n = _sub_pattern(pat, text, REDACT_PRIVATE)
            if n:
                reasons.append("private_phrase")

        for pat in self._name_patterns:
            text, n = _sub_pattern(pat, text, REDACT_NAME)
            if n:
                reasons.append("blocked_name")

        scrubbed = re.sub(r"\s+", " ", text).strip()
        blocked = not _has_searchable_tokens(scrubbed)
        if blocked and original:
            reasons.append("blocked_after_scrub")
        return ScrubResult(
            original=original,
            scrubbed=scrubbed,
            blocked=blocked,
            reasons=tuple(dict.fromkeys(reasons)),
        )


def _sub_all(pattern: re.Pattern[str], text: str, repl: str) -> tuple[str, int]:
    new, n = pattern.subn(repl, text)
    return new, n


def _sub_pattern(pattern: re.Pattern[str], text: str, repl: str) -> tuple[str, int]:
    new, n = pattern.subn(repl, text)
    return new, n


def _normalize_phrase(text: str) -> str:
    return " ".join(_TOKEN_RE.findall(text.lower()))


def _flex_phrase_pattern(phrase: str) -> re.Pattern[str]:
    words = _TOKEN_RE.findall(phrase.lower())
    if not words:
        return re.compile(r"(?!x)x")
    body = r"\s+".join(re.escape(w) for w in words)
    return re.compile(rf"\b{body}\b", re.I)


def _has_searchable_tokens(scrubbed: str) -> bool:
    stripped = scrubbed
    for token in (
        REDACT_EMAIL,
        REDACT_PHONE,
        REDACT_ADDRESS,
        REDACT_NAME,
        REDACT_PRIVATE,
        REDACT_ACCOUNT,
    ):
        stripped = stripped.replace(token, " ")
    stripped = re.sub(r"\[[A-Za-z_]+\]", " ", stripped)
    tokens = _TOKEN_RE.findall(stripped.lower())
    return sum(len(t) for t in tokens) >= 2


def name_from_notion_segment(segment: str) -> str | None:
    m = _NOTION_SEGMENT.match(segment.strip())
    if not m:
        return None
    name = m.group(1).strip()
    if not name or name.lower() in {"people", "index", "friends"}:
        return None
    return name


def names_from_source_path(path: str) -> list[str]:
    out: list[str] = []
    norm = path.replace("\\", "/")
    if "People [" not in norm and not norm.startswith("People/"):
        return out
    for part in norm.split("/"):
        n = name_from_notion_segment(part)
        if n:
            out.append(n)
    return out


def extract_private_phrases(texts: Iterable[str], *, min_words: int = _MIN_NGRAM_WORDS) -> list[str]:
    phrases: set[str] = set()
    for raw in texts:
        norm = _normalize_phrase(raw)
        words = norm.split()
        if len(words) < min_words:
            continue
        for i in range(len(words) - min_words + 1):
            for size in range(min_words, min(len(words) - i, 12) + 1):
                phrases.add(" ".join(words[i : i + size]))
    return sorted(phrases, key=len, reverse=True)


def load_contact_names() -> list[str]:
    path = duckdb_path()
    if not path.is_file():
        return []
    try:
        con = duckdb.connect(str(path), read_only=True)
        try:
            rows = con.execute(
                """
                SELECT DISTINCT COALESCE(text, title) AS name
                FROM events
                WHERE product = 'contacts' AND COALESCE(text, title) IS NOT NULL
                """
            ).fetchall()
        except duckdb.Error:
            return []
        finally:
            con.close()
    except OSError:
        return []
    return [str(r[0]).strip() for r in rows if r and r[0]]


def load_people_names_from_db() -> list[str]:
    try:
        from virtual_self.persistence.db import connection, fetch_all, init_pool

        init_pool()
        with connection() as conn:
            rows = fetch_all(
                conn,
                """
                SELECT DISTINCT s.source_path
                FROM sources s
                WHERE s.source_path ILIKE '%People [%'
                LIMIT 5000
                """,
            )
    except Exception:
        return []
    names: list[str] = []
    for row in rows:
        path = row.get("source_path") or ""
        names.extend(names_from_source_path(str(path)))
    return names


def load_private_memory_texts() -> list[str]:
    try:
        from virtual_self.persistence.db import connection, fetch_all, init_pool

        init_pool()
        with connection() as conn:
            rows = fetch_all(
                conn,
                """
                SELECT mv.content
                FROM memory_versions mv
                WHERE mv.status = 'active' AND mv.sensitivity >= %s AND mv.content IS NOT NULL
                LIMIT 8000
                """,
                (SENSITIVITY_PRIVATE,),
            )
    except Exception:
        return []
    return [str(r["content"]) for r in rows if r.get("content")]


def build_query_scrubber(
    *,
    extra_names: Iterable[str] | None = None,
    extra_private_texts: Iterable[str] | None = None,
) -> QueryScrubber:
    names: set[str] = set(load_contact_names())
    names.update(load_people_names_from_db())
    if extra_names:
        names.update(n.strip() for n in extra_names if n and n.strip())

    private_texts = load_private_memory_texts()
    if extra_private_texts:
        private_texts = list(private_texts) + [t for t in extra_private_texts if t]

    phrases = extract_private_phrases(private_texts)
    log.debug(
        "query_scrubber_built",
        name_count=len(names),
        phrase_count=len(phrases),
    )
    return QueryScrubber(blocked_names=tuple(names), private_phrases=tuple(phrases))


def load_scrubber_from_fixture(path: Path) -> QueryScrubber:
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    return QueryScrubber(
        blocked_names=tuple(data.get("blocked_names") or []),
        private_phrases=tuple(data.get("private_phrases") or []),
    )


def contains_blocked_secret(scrubbed: str, secrets: Iterable[str]) -> str | None:
    """Return the first secret still present in scrubbed (case-insensitive), or None."""
    low = scrubbed.lower()
    for s in secrets:
        if not s:
            continue
        if s.lower() in low:
            return s
    return None
