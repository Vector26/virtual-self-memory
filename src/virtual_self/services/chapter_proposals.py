"""Propose life chapters from dated memories (T-4). Writes status=proposed only."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from uuid6 import uuid7

from virtual_self.auth.credentials import AuthContext
from virtual_self.persistence.db import connection, execute, fetch_all, fetch_one, transaction
from virtual_self.providers.agent_chat import chat_json_for_task

MIN_PROPOSALS_DEFAULT = 5
MAX_PROPOSALS_DEFAULT = 15
GAP_DAYS_DEFAULT = 120
MIN_MEMORIES_PER_SEGMENT = 2

_STRAND_KEYWORDS: dict[str, tuple[str, ...]] = {
    "relationships": (
        "relationship",
        "breakup",
        "girlfriend",
        "boyfriend",
        "partner",
        "dating",
        "marriage",
        "college",
        "school",
        "love",
        "wedding",
    ),
    "career": (
        "job",
        "work",
        "office",
        "startup",
        "employer",
        "salary",
        "promotion",
        "interview",
        "manager",
        "company",
    ),
    "mind": (
        "therapy",
        "mental",
        "health",
        "anxiety",
        "depression",
        "grief",
        "burnout",
        "meditation",
        "journal",
    ),
    "craft": (
        "project",
        "code",
        "coding",
        "lab",
        "homelab",
        "hobby",
        "build",
        "writing",
        "music",
        "art",
    ),
    "place": (
        "moved",
        "move",
        "city",
        "apartment",
        "house",
        "travel",
        "relocate",
        "neighborhood",
        "country",
    ),
}

_EXPLICIT_CHAPTER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "place",
        re.compile(
            r"\b(moved to|moving to|new city|relocated to|shifted to)\b",
            re.I,
        ),
    ),
    (
        "relationships",
        re.compile(r"\b(breakup|broke up|post-breakup|after the breakup)\b", re.I),
    ),
    (
        "relationships",
        re.compile(r"\b(school|college)\b.*\b(relationship|dating|girlfriend|boyfriend)\b", re.I),
    ),
)

_LLM_SYSTEM = """You name life chapters for one person from memory snippets.
Return JSON only: {"title":"short chapter name","summary":"one sentence","precision":"year|month|day"}.
Never invent calendar dates not supported by the snippets. Prefer coarse precision when unsure."""


@dataclass
class EpisodeRow:
    memory_id: UUID
    content: str
    event_at: datetime
    time_precision: str
    scope: str


@dataclass
class ChapterDraft:
    strand_slug: str
    title: str
    summary: str
    valid_from: datetime | None
    valid_to: datetime | None
    time_precision: str
    valid_from_precision: str
    valid_to_precision: str
    edge_fuzz_days: int
    memory_ids: list[UUID]
    source: str = "rules"

    def dedupe_key(self) -> tuple[str, str]:
        norm = re.sub(r"\s+", " ", self.title.strip().lower())
        return (self.strand_slug, norm)


@dataclass
class ChapterProposalReport:
    dry_run: bool
    use_llm: bool
    dated_episodes: int = 0
    candidates: int = 0
    inserted: int = 0
    skipped_dedupe: int = 0
    skipped_existing: int = 0
    proposals: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "use_llm": self.use_llm,
            "dated_episodes": self.dated_episodes,
            "candidates": self.candidates,
            "inserted": self.inserted,
            "skipped_dedupe": self.skipped_dedupe,
            "skipped_existing": self.skipped_existing,
            "proposal_count": len(self.proposals),
            "proposals": self.proposals,
        }


def _coarse_precision(values: set[str]) -> str:
    order = ("instant", "day", "month", "year", "chapter", "unknown")
    present = [p for p in values if p in order]
    if not present:
        return "unknown"
    return max(present, key=lambda p: order.index(p))


def _precision_fuzz_days(precision: str) -> int:
    return {"year": 180, "month": 15, "day": 2, "instant": 0, "chapter": 30}.get(precision, 0)


def _month_floor(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _year_floor(dt: datetime) -> datetime:
    return dt.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)


def _normalize_bounds(
    episodes: list[EpisodeRow],
) -> tuple[datetime | None, datetime | None, str, str, str, int]:
    if not episodes:
        return None, None, "unknown", "unknown", "unknown", 0
    prec = _coarse_precision({e.time_precision for e in episodes})
    times = sorted(e.event_at for e in episodes)
    start, end = times[0], times[-1]
    if prec == "year":
        start, end = _year_floor(start), _year_floor(end)
    elif prec == "month":
        start, end = _month_floor(start), _month_floor(end)
    fuzz = _precision_fuzz_days(prec)
    return start, end, prec, prec, prec, fuzz


def score_strand(text: str) -> str:
    lower = text.lower()
    scores: dict[str, int] = {slug: 0 for slug in _STRAND_KEYWORDS}
    for slug, words in _STRAND_KEYWORDS.items():
        for w in words:
            if w in lower:
                scores[slug] += 1
    for slug, pat in _EXPLICIT_CHAPTER_PATTERNS:
        if pat.search(text):
            scores[slug] += 3
    best = max(scores.items(), key=lambda kv: kv[1])
    return best[0] if best[1] > 0 else "mind"


def _rules_title(strand_slug: str, episodes: list[EpisodeRow]) -> tuple[str, str]:
    text = " ".join(e.content for e in episodes[:8])
    lower = text.lower()
    years = sorted({e.event_at.year for e in episodes})
    yr = f"{years[0]}–{years[-1]}" if len(years) > 1 else str(years[0])
    if "breakup" in lower or "post-breakup" in lower:
        return f"Post-breakup rebuild ({yr})", "Rebuild period after relationship ended."
    if re.search(r"\b(moved to|new city|relocated)\b", lower):
        return f"New city ({yr})", "Relocation / new place chapter."
    if re.search(r"\b(school|college)\b", lower) and re.search(
        r"\b(relationship|dating|girlfriend|boyfriend)\b", lower
    ):
        return f"School + college relationship era ({yr})", "Relationship spanning school/college years."
    strand_label = strand_slug.replace("_", " ")
    return f"{strand_label.title()} · {yr}", f"Clustered {strand_label} memories ({yr})."


def _llm_title(episodes: list[EpisodeRow]) -> tuple[str, str, str] | None:
    snippets = []
    for ep in episodes[:12]:
        snippets.append(f"- {ep.event_at.date().isoformat()} ({ep.time_precision}): {ep.content[:200]}")
    user = "Memory snippets:\n" + "\n".join(snippets)
    parsed = chat_json_for_task(_LLM_SYSTEM, user, temperature=0.1)
    if not parsed:
        return None
    title = str(parsed.get("title") or "").strip()
    summary = str(parsed.get("summary") or "").strip()
    precision = str(parsed.get("precision") or "year").strip()
    if precision not in ("year", "month", "day", "instant", "unknown"):
        precision = "year"
    if not title:
        return None
    return title, summary or title, precision


def _segment_episodes(episodes: list[EpisodeRow], gap_days: int) -> list[list[EpisodeRow]]:
    if not episodes:
        return []
    episodes = sorted(episodes, key=lambda e: e.event_at)
    segments: list[list[EpisodeRow]] = [[episodes[0]]]
    for ep in episodes[1:]:
        prev = segments[-1][-1]
        delta = (ep.event_at - prev.event_at).days
        if delta >= gap_days:
            segments.append([ep])
        else:
            segments[-1].append(ep)
    merged: list[list[EpisodeRow]] = []
    for seg in segments:
        if len(seg) < MIN_MEMORIES_PER_SEGMENT and merged:
            merged[-1].extend(seg)
        else:
            merged.append(seg)
    return [s for s in merged if len(s) >= MIN_MEMORIES_PER_SEGMENT]


def _draft_from_segment(
    strand_slug: str,
    episodes: list[EpisodeRow],
    *,
    use_llm: bool,
    source: str | None = None,
) -> ChapterDraft:
    title, summary = _rules_title(strand_slug, episodes)
    precision_override: str | None = None
    if use_llm:
        llm = _llm_title(episodes)
        if llm:
            title, summary, precision_override = llm
    vf, vt, prec, vfp, vtp, fuzz = _normalize_bounds(episodes)
    if precision_override:
        prec = precision_override
        vfp = vtp = precision_override
        fuzz = _precision_fuzz_days(precision_override)
    return ChapterDraft(
        strand_slug=strand_slug,
        title=title,
        summary=summary,
        valid_from=vf,
        valid_to=vt if len(episodes) >= MIN_MEMORIES_PER_SEGMENT else None,
        time_precision=prec,
        valid_from_precision=vfp,
        valid_to_precision=vtp,
        edge_fuzz_days=fuzz,
        memory_ids=[e.memory_id for e in episodes],
        source=source or ("llm" if use_llm and precision_override else "rules"),
    )


def _explicit_signal_drafts(episodes: list[EpisodeRow]) -> list[ChapterDraft]:
    drafts: list[ChapterDraft] = []
    for strand, pat in _EXPLICIT_CHAPTER_PATTERNS:
        hits = [e for e in episodes if pat.search(e.content)]
        if len(hits) < 1:
            continue
        drafts.append(_draft_from_segment(strand, hits, use_llm=False, source="explicit"))
    return drafts


def _draft_sort_key(d: ChapterDraft) -> tuple[int, str, datetime]:
    return (
        0 if d.source == "explicit" else 1,
        d.strand_slug,
        d.valid_from or datetime.min.replace(tzinfo=timezone.utc),
    )


def _dedupe_drafts(drafts: list[ChapterDraft]) -> tuple[list[ChapterDraft], int]:
    seen: set[tuple[str, str]] = set()
    out: list[ChapterDraft] = []
    skipped = 0
    for d in sorted(drafts, key=lambda x: (-len(x.memory_ids), x.strand_slug)):
        key = d.dedupe_key()
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        out.append(d)
    return out, skipped


def _load_dated_episodes(conn, subject_id: UUID) -> list[EpisodeRow]:
    rows = fetch_all(
        conn,
        """
        SELECT
          m.id AS memory_id,
          mv.content,
          m.scope,
          m.time_precision,
          COALESCE(m.valid_from, m.authored_at, mv.event_time, mv.valid_from) AS event_at
        FROM memories m
        JOIN artifacts a ON a.id = m.id AND a.deleted_at IS NULL
        JOIN memory_versions mv ON mv.id = m.current_version_id AND mv.status = 'active'
        WHERE m.subject_id = %s
          AND m.time_precision <> 'unknown'
          AND COALESCE(m.valid_from, m.authored_at, mv.event_time, mv.valid_from) IS NOT NULL
        ORDER BY event_at ASC
        """,
        (subject_id,),
    )
    out: list[EpisodeRow] = []
    for r in rows:
        event_at = r["event_at"]
        if event_at.tzinfo is None:
            event_at = event_at.replace(tzinfo=timezone.utc)
        out.append(
            EpisodeRow(
                memory_id=r["memory_id"],
                content=r["content"] or "",
                event_at=event_at,
                time_precision=r["time_precision"] or "unknown",
                scope=r["scope"] or "",
            )
        )
    return out


def _existing_proposed_keys(conn, subject_id: UUID) -> set[tuple[str, str]]:
    rows = fetch_all(
        conn,
        """
        SELECT lc.title, ls.slug
        FROM life_chapters lc
        JOIN life_strands ls ON ls.id = lc.strand_id
        WHERE lc.subject_id = %s AND lc.status = 'proposed'
        """,
        (subject_id,),
    )
    keys: set[tuple[str, str]] = set()
    for r in rows:
        norm = re.sub(r"\s+", " ", (r["title"] or "").strip().lower())
        keys.add((r["slug"], norm))
    return keys


def _strand_id(conn, slug: str) -> UUID:
    row = fetch_one(conn, "SELECT id FROM life_strands WHERE slug = %s", (slug,))
    if not row:
        raise ValueError(f"unknown strand slug: {slug}")
    return row["id"]


def _persist_proposal(conn, subject_id: UUID, draft: ChapterDraft) -> UUID:
    chapter_id = uuid7()
    strand_id = _strand_id(conn, draft.strand_slug)
    execute(
        conn,
        """
        INSERT INTO life_chapters (
          id, subject_id, strand_id, title, summary, status,
          valid_from, valid_to, time_precision,
          valid_from_precision, valid_to_precision, edge_fuzz_days
        ) VALUES (
          %s, %s, %s, %s, %s, 'proposed',
          %s, %s, %s, %s, %s, %s
        )
        """,
        (
            chapter_id,
            subject_id,
            strand_id,
            draft.title,
            draft.summary,
            draft.valid_from,
            draft.valid_to,
            draft.time_precision,
            draft.valid_from_precision,
            draft.valid_to_precision,
            draft.edge_fuzz_days,
        ),
    )
    for mid in draft.memory_ids:
        execute(
            conn,
            """
            INSERT INTO life_chapter_evidence (
              id, chapter_id, memory_id, relationship
            ) VALUES (%s, %s, %s, 'supporting')
            ON CONFLICT (chapter_id, memory_id) DO NOTHING
            """,
            (uuid7(), chapter_id, mid),
        )
    return chapter_id


def build_chapter_drafts(
    episodes: list[EpisodeRow],
    *,
    use_llm: bool = False,
    gap_days: int = GAP_DAYS_DEFAULT,
    max_proposals: int = MAX_PROPOSALS_DEFAULT,
) -> list[ChapterDraft]:
    by_strand: dict[str, list[EpisodeRow]] = {}
    for ep in episodes:
        slug = score_strand(ep.content)
        by_strand.setdefault(slug, []).append(ep)

    drafts: list[ChapterDraft] = _explicit_signal_drafts(episodes)
    for slug, eps in by_strand.items():
        for seg in _segment_episodes(eps, gap_days):
            drafts.append(_draft_from_segment(slug, seg, use_llm=use_llm))

    drafts, _ = _dedupe_drafts(drafts)
    drafts.sort(key=_draft_sort_key)
    return drafts


def run_chapter_proposals(
    auth: AuthContext,
    *,
    dry_run: bool = True,
    use_llm: bool = False,
    min_proposals: int = MIN_PROPOSALS_DEFAULT,
    max_proposals: int = MAX_PROPOSALS_DEFAULT,
    gap_days: int = GAP_DAYS_DEFAULT,
    audit_path: Path | None = None,
) -> ChapterProposalReport:
    report = ChapterProposalReport(dry_run=dry_run, use_llm=use_llm)
    with connection() as conn:
        episodes = _load_dated_episodes(conn, auth.subject_id)
        existing = _existing_proposed_keys(conn, auth.subject_id)

    report.dated_episodes = len(episodes)
    drafts = sorted(
        build_chapter_drafts(
            episodes,
            use_llm=use_llm,
            gap_days=gap_days,
            max_proposals=max_proposals,
        ),
        key=_draft_sort_key,
    )
    report.candidates = len(drafts)

    final: list[ChapterDraft] = []
    for d in drafts:
        if d.dedupe_key() in existing:
            report.skipped_existing += 1
            continue
        final.append(d)
    final, dedupe_sk = _dedupe_drafts(final)
    report.skipped_dedupe += dedupe_sk
    final = final[:max_proposals]

    if len(final) < min_proposals and len(final) < len(drafts):
        # Prefer filling toward min_proposals without bypassing dedupe/existing filters.
        for d in drafts:
            if len(final) >= min_proposals or len(final) >= max_proposals:
                break
            if d in final:
                continue
            if d.dedupe_key() in existing:
                continue
            if any(x.dedupe_key() == d.dedupe_key() for x in final):
                continue
            final.append(d)

    def _append_entry(d: ChapterDraft, chapter_id: UUID | None) -> None:
        entry: dict[str, Any] = {
            "strand": d.strand_slug,
            "title": d.title,
            "summary": d.summary,
            "valid_from": d.valid_from.isoformat() if d.valid_from else None,
            "valid_to": d.valid_to.isoformat() if d.valid_to else None,
            "time_precision": d.time_precision,
            "evidence_memory_ids": [str(x) for x in d.memory_ids[:20]],
            "evidence_count": len(d.memory_ids),
            "source": d.source,
        }
        if chapter_id is not None:
            entry["chapter_id"] = str(chapter_id)
        report.proposals.append(entry)

    if dry_run:
        for d in final:
            _append_entry(d, None)
    else:
        from virtual_self.services.chapter_proposal_notify import notify_chapter_proposal

        with transaction() as conn:
            for d in final:
                cid = _persist_proposal(conn, auth.subject_id, d)
                report.inserted += 1
                _append_entry(d, cid)
                notify_chapter_proposal(
                    chapter_id=cid,
                    strand=d.strand_slug,
                    title=d.title,
                    summary=d.summary,
                    valid_from=d.valid_from,
                    valid_to=d.valid_to,
                    evidence_count=len(d.memory_ids),
                )

    if audit_path is not None:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    return report
