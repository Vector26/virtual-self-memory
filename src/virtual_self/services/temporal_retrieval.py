"""Chapter-scoped, validity-aware retrieval (Phase T / §15.5)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from virtual_self.persistence.db import fetch_all

TemporalKind = Literal["none", "now", "then", "date", "chapter", "always"]

_NOW_RE = re.compile(
    r"(?is)\b(now|currently|these days|today|right now|at the moment|present day)\b"
)
_THEN_RE = re.compile(r"(?is)\b(back then|at the time|those days|in those days)\b")
_ALWAYS_RE = re.compile(r"(?is)\b(always|ever|my whole life|all my life|throughout)\b")
_YEAR_RE = re.compile(r"(?is)\b(?:in\s+)?((?:19|20)\d{2})\b")
_MID_YEAR_RE = re.compile(r"(?is)\bmid[- ]?((?:19|20)\d{2})\b")


@dataclass(frozen=True)
class TemporalIntent:
    kind: TemporalKind = "none"
    as_of: datetime | None = None
    chapter_title_hint: str | None = None


def parse_temporal_intent(query: str) -> TemporalIntent:
    """Lightweight intent parse from user text (no DB)."""
    text = query.strip()
    if not text:
        return TemporalIntent()

    mid = _MID_YEAR_RE.search(text)
    if mid:
        year = int(mid.group(1))
        return TemporalIntent(
            kind="date",
            as_of=datetime(year, 7, 1, tzinfo=timezone.utc),
        )

    year_m = _YEAR_RE.search(text)
    if year_m:
        year = int(year_m.group(1))
        return TemporalIntent(
            kind="date",
            as_of=datetime(year, 7, 1, tzinfo=timezone.utc),
        )

    if _ALWAYS_RE.search(text):
        return TemporalIntent(kind="always")

    if _NOW_RE.search(text):
        return TemporalIntent(kind="now", as_of=datetime.now(timezone.utc))

    if _THEN_RE.search(text):
        return TemporalIntent(kind="then", as_of=datetime.now(timezone.utc))

    return TemporalIntent()


def match_chapters_in_query(
    conn,
    subject_id: UUID,
    query: str,
) -> list[dict[str, Any]]:
    """Active chapters whose title appears in the query (case-insensitive)."""
    rows = fetch_all(
        conn,
        """
        SELECT lc.id, lc.title, lc.summary, lc.valid_from, lc.valid_to,
               lc.time_precision, lc.valid_from_precision, lc.valid_to_precision,
               ls.slug AS strand_slug, ls.display_name AS strand_name
        FROM life_chapters lc
        JOIN life_strands ls ON ls.id = lc.strand_id
        WHERE lc.subject_id = %s AND lc.status = 'active'
        ORDER BY LENGTH(lc.title) DESC
        """,
        (subject_id,),
    )
    q_cf = query.casefold()
    hits: list[dict[str, Any]] = []
    for row in rows:
        title = (row.get("title") or "").strip()
        if len(title) >= 4 and title.casefold() in q_cf:
            hits.append(dict(row))
    return hits


def refine_intent_with_chapters(
    intent: TemporalIntent,
    chapter_hits: list[dict[str, Any]],
) -> TemporalIntent:
    if not chapter_hits:
        return intent
    primary = chapter_hits[0]
    as_of = intent.as_of
    if as_of is None and primary.get("valid_from"):
        as_of = primary["valid_from"]
    return TemporalIntent(
        kind="chapter",
        as_of=as_of,
        chapter_title_hint=primary.get("title"),
    )


def select_chapters_for_intent(
    conn,
    subject_id: UUID,
    intent: TemporalIntent,
    *,
    title_matched: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if intent.kind == "none":
        return []

    if intent.kind == "chapter" and title_matched:
        return title_matched

    now = datetime.now(timezone.utc)
    as_of = intent.as_of or now

    if intent.kind == "always":
        rows = fetch_all(
            conn,
            """
            SELECT lc.id, lc.title, lc.summary, lc.valid_from, lc.valid_to,
                   lc.time_precision, lc.valid_from_precision, lc.valid_to_precision,
                   ls.slug AS strand_slug, ls.display_name AS strand_name
            FROM life_chapters lc
            JOIN life_strands ls ON ls.id = lc.strand_id
            WHERE lc.subject_id = %s AND lc.status = 'active'
            ORDER BY lc.valid_from NULLS LAST, lc.title
            """,
            (subject_id,),
        )
        return [dict(r) for r in rows]

    if intent.kind == "then" and intent.as_of == now and title_matched is None:
        rows = fetch_all(
            conn,
            """
            SELECT lc.id, lc.title, lc.summary, lc.valid_from, lc.valid_to,
                   lc.time_precision, lc.valid_from_precision, lc.valid_to_precision,
                   ls.slug AS strand_slug, ls.display_name AS strand_name
            FROM life_chapters lc
            JOIN life_strands ls ON ls.id = lc.strand_id
            WHERE lc.subject_id = %s AND lc.status = 'active'
              AND lc.valid_to IS NOT NULL AND lc.valid_to <= %s
            ORDER BY lc.valid_to DESC
            LIMIT 8
            """,
            (subject_id, now),
        )
        if rows:
            return [dict(r) for r in rows]

    rows = fetch_all(
        conn,
        """
        SELECT lc.id, lc.title, lc.summary, lc.valid_from, lc.valid_to,
               lc.time_precision, lc.valid_from_precision, lc.valid_to_precision,
               ls.slug AS strand_slug, ls.display_name AS strand_name
        FROM life_chapters lc
        JOIN life_strands ls ON ls.id = lc.strand_id
        WHERE lc.subject_id = %s AND lc.status = 'active'
          AND (lc.valid_from IS NULL OR lc.valid_from <= %s)
          AND (lc.valid_to IS NULL OR lc.valid_to >= %s)
        ORDER BY lc.valid_from DESC NULLS LAST
        LIMIT 12
        """,
        (subject_id, as_of, as_of),
    )
    merged = _merge_chapter_lists(title_matched or [], [dict(r) for r in rows])
    return merged[:12]


def _merge_chapter_lists(
    primary: list[dict[str, Any]],
    secondary: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in primary + secondary:
        cid = str(row["id"])
        if cid in seen:
            continue
        seen.add(cid)
        out.append(row)
    return out


def fetch_patterns_for_chapters(
    conn,
    subject_id: UUID,
    chapter_ids: list[UUID],
) -> list[dict[str, Any]]:
    if not chapter_ids:
        return []
    rows = fetch_all(
        conn,
        """
        SELECT lp.id, lp.chapter_id, lp.name, lp.description, lp.time_precision,
               lp.valid_from, lp.valid_to
        FROM life_patterns lp
        WHERE lp.subject_id = %s
          AND lp.chapter_id = ANY(%s::uuid[])
          AND lp.status IN ('active', 'proposed')
        ORDER BY lp.name
        """,
        (subject_id, chapter_ids),
    )
    return [dict(r) for r in rows]


def format_chapter_time_label(chapter: dict[str, Any]) -> str:
    """Bundle header: time · title · precision (no false day precision)."""
    title = (chapter.get("title") or "chapter").strip()
    prec = (chapter.get("time_precision") or "unknown").strip()
    vf = chapter.get("valid_from")
    time_part = ""
    if vf is not None:
        if prec == "year":
            time_part = str(vf.year)
        elif prec == "month":
            time_part = vf.strftime("%Y-%m")
        elif prec in ("day", "instant"):
            time_part = vf.date().isoformat()
        elif prec == "chapter":
            time_part = title
        else:
            time_part = str(vf.year) if hasattr(vf, "year") else ""

    parts: list[str] = []
    if time_part and time_part != title:
        parts.append(time_part)
    parts.append(title)
    if prec and prec != "unknown":
        parts.append(f"{prec} precision")
    return " · ".join(parts)


def memory_valid_at(
    *,
    valid_from: datetime | None,
    valid_to: datetime | None,
    invalidated_at: datetime | None,
    as_of: datetime,
) -> bool:
    if invalidated_at is not None and invalidated_at <= as_of:
        return False
    if valid_from is not None and valid_from > as_of:
        return False
    if valid_to is not None and valid_to <= as_of:
        return False
    return True


@dataclass
class ChapterMemoryGroup:
    chapter_id: str | None
    header: str
    patterns: list[str] = field(default_factory=list)
    memories: list[dict[str, Any]] = field(default_factory=list)


def group_memories_by_chapter(
    memories: list[dict[str, Any]],
    chapters: list[dict[str, Any]],
    patterns: list[dict[str, Any]],
) -> list[ChapterMemoryGroup]:
    by_id = {str(c["id"]): c for c in chapters}
    pattern_lines: dict[str, list[str]] = {}
    for p in patterns:
        cid = str(p["chapter_id"])
        line = p.get("name") or ""
        desc = (p.get("description") or "").strip()
        if desc:
            line = f"{line}: {desc}" if line else desc
        if line:
            pattern_lines.setdefault(cid, []).append(line)

    buckets: dict[str | None, list[dict[str, Any]]] = {}
    for m in memories:
        cid = m.get("chapter_id")
        key = str(cid) if cid else None
        buckets.setdefault(key, []).append(m)

    groups: list[ChapterMemoryGroup] = []
    seen_keys: set[str | None] = set()

    for ch in chapters:
        cid = str(ch["id"])
        seen_keys.add(cid)
        mems = buckets.get(cid, [])
        pats = pattern_lines.get(cid, [])
        if not mems and not pats:
            continue
        header = format_chapter_time_label(ch)
        groups.append(
            ChapterMemoryGroup(
                chapter_id=cid,
                header=header,
                patterns=pats,
                memories=mems,
            )
        )

    orphan = buckets.get(None, [])
    if orphan:
        groups.append(
            ChapterMemoryGroup(
                chapter_id=None,
                header="Ungrouped · unknown precision",
                memories=orphan,
            )
        )

    for cid, mems in buckets.items():
        if cid in seen_keys or cid is None:
            continue
        ch = by_id.get(cid)
        header = format_chapter_time_label(ch) if ch else f"Chapter {cid[:8]}"
        groups.append(
            ChapterMemoryGroup(
                chapter_id=cid,
                header=header,
                patterns=pattern_lines.get(cid, []),
                memories=mems,
            )
        )

    return groups


def chapter_groups_to_bundle(groups: list[ChapterMemoryGroup]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for g in groups:
        out.append(
            {
                "chapter_id": g.chapter_id,
                "header": g.header,
                "patterns": g.patterns,
                "memories": g.memories,
            }
        )
    return out
