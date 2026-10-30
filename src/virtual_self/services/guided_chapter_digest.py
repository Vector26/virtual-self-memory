"""Per-chapter guided digest synthesis with memory-id evidence (TR-2)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from uuid6 import uuid7

from virtual_self.auth.credentials import AuthContext
from virtual_self.domain.errors import NotFoundError, ValidationError
from virtual_self.persistence.db import execute, fetch_all, fetch_one, transaction
from virtual_self.providers.agent_chat import chat_json_for_task
from virtual_self.services.guided_digest_prompts import (
    GUIDED_DIGEST_SYSTEM,
    GUIDED_QUESTIONS,
    PROMPT_VERSION,
)

DigestStatus = Literal["draft", "proposed"]
_JOURNAL_HINT = re.compile(r"\b(grief|anxiet|depress|exhaust|burnout|lonely|sad|therapy)\b", re.I)


@dataclass(frozen=True)
class MemorySnippet:
    memory_id: UUID
    content: str
    register: str | None
    scope: str | None


@dataclass(frozen=True)
class ChapterTarget:
    chapter_id: UUID
    title: str
    summary: str | None
    strand_slug: str
    valid_from: datetime | None
    valid_to: datetime | None
    status: str


@dataclass
class GuidedDigestReport:
    chapters_seen: int = 0
    digests_written: int = 0
    skipped_existing: int = 0
    skipped_thin: int = 0
    dry_run: bool = True
    use_llm: bool = False
    digests: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapters_seen": self.chapters_seen,
            "digests_written": self.digests_written,
            "skipped_existing": self.skipped_existing,
            "skipped_thin": self.skipped_thin,
            "dry_run": self.dry_run,
            "use_llm": self.use_llm,
            "digests": self.digests,
        }


def _normalize_ids(raw: Any, allowed: set[str]) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        sid = str(item).strip()
        if sid in allowed and sid not in out:
            out.append(sid)
    return out


def _journaling_bias_note(snippets: list[MemorySnippet]) -> str:
    if not snippets:
        return ""
    journalish = sum(
        1
        for s in snippets
        if (s.register or "") == "journal" or _JOURNAL_HINT.search(s.content or "")
    )
    if journalish >= max(2, len(snippets) // 2):
        return (
            "Journal-weighted chapter bundle: low-mood or journal register may oversample "
            "grief/strain — treat trait reads as context-biased."
        )
    return ""


def _load_chapter_targets(conn, subject_id: UUID, *, chapter_status: str) -> list[ChapterTarget]:
    rows = fetch_all(
        conn,
        """
        SELECT lc.id, lc.title, lc.summary, lc.status,
               lc.valid_from, lc.valid_to, ls.slug AS strand_slug
        FROM life_chapters lc
        JOIN life_strands ls ON ls.id = lc.strand_id
        WHERE lc.subject_id = %s AND lc.status = %s
        ORDER BY lc.valid_from NULLS LAST, lc.title
        """,
        (subject_id, chapter_status),
    )
    out: list[ChapterTarget] = []
    for r in rows:
        vf, vt = r["valid_from"], r["valid_to"]
        if vf is not None and vf.tzinfo is None:
            vf = vf.replace(tzinfo=timezone.utc)
        if vt is not None and vt.tzinfo is None:
            vt = vt.replace(tzinfo=timezone.utc)
        out.append(
            ChapterTarget(
                chapter_id=r["id"],
                title=r["title"] or "",
                summary=r["summary"],
                strand_slug=r["strand_slug"],
                valid_from=vf,
                valid_to=vt,
                status=r["status"],
            )
        )
    return out


def _load_chapter_memory_snippets(conn, subject_id: UUID, chapter_id: UUID) -> list[MemorySnippet]:
    rows = fetch_all(
        conn,
        """
        SELECT m.id AS memory_id, mv.content, m.scope, NULL::text AS register
        FROM life_chapter_evidence lce
        JOIN memories m ON m.id = lce.memory_id
        JOIN artifacts a ON a.id = m.id AND a.deleted_at IS NULL
        JOIN memory_versions mv ON mv.id = m.current_version_id AND mv.status = 'active'
        WHERE lce.chapter_id = %s AND m.subject_id = %s
        ORDER BY COALESCE(m.valid_from, m.authored_at) NULLS LAST, m.id
        """,
        (chapter_id, subject_id),
    )
    if rows:
        return [
            MemorySnippet(
                memory_id=r["memory_id"],
                content=str(r["content"] or ""),
                register=str(r["register"]) if r.get("register") else None,
                scope=str(r["scope"]) if r.get("scope") else None,
            )
            for r in rows
        ]

    chapter = fetch_one(
        conn,
        "SELECT valid_from, valid_to FROM life_chapters WHERE id = %s",
        (chapter_id,),
    )
    if chapter is None:
        return []
    rows = fetch_all(
        conn,
        """
        SELECT m.id AS memory_id, mv.content, m.scope, NULL::text AS register
        FROM memories m
        JOIN artifacts a ON a.id = m.id AND a.deleted_at IS NULL
        JOIN memory_versions mv ON mv.id = m.current_version_id AND mv.status = 'active'
        WHERE m.subject_id = %s
          AND m.time_precision <> 'unknown'
          AND COALESCE(m.valid_from, m.authored_at, mv.event_time) IS NOT NULL
          AND (%s::timestamptz IS NULL OR COALESCE(m.valid_from, m.authored_at, mv.event_time) >= %s)
          AND (%s::timestamptz IS NULL OR COALESCE(m.valid_from, m.authored_at, mv.event_time) <= %s)
        ORDER BY COALESCE(m.valid_from, m.authored_at, mv.event_time) ASC
        LIMIT 40
        """,
        (
            subject_id,
            chapter["valid_from"],
            chapter["valid_from"],
            chapter["valid_to"],
            chapter["valid_to"],
        ),
    )
    return [
        MemorySnippet(
            memory_id=r["memory_id"],
            content=str(r["content"] or ""),
            register=None,
            scope=str(r["scope"]) if r.get("scope") else None,
        )
        for r in rows
    ]


def _bundle_for_llm(chapter: ChapterTarget, snippets: list[MemorySnippet]) -> str:
    lines = [
        f"Chapter: {chapter.title} ({chapter.strand_slug})",
        f"Window: {chapter.valid_from} .. {chapter.valid_to}",
        f"Summary: {chapter.summary or ''}",
        "",
        "Memories (cite memory_id only):",
    ]
    for s in snippets:
        excerpt = re.sub(r"\s+", " ", s.content).strip()[:480]
        lines.append(
            json.dumps(
                {
                    "memory_id": str(s.memory_id),
                    "register": s.register,
                    "scope": s.scope,
                    "excerpt": excerpt,
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(lines)


def _deterministic_digest(
    chapter: ChapterTarget, snippets: list[MemorySnippet]
) -> dict[str, Any]:
    allowed = {str(s.memory_id) for s in snippets}
    pick = str(snippets[0].memory_id) if snippets else ""
    guided: list[dict[str, Any]] = []
    profile: list[dict[str, Any]] = []
    sort_i = 0
    for layer, questions in GUIDED_QUESTIONS.items():
        for qkey, qtext in questions:
            excerpt = re.sub(r"\s+", " ", snippets[0].content).strip()[:220] if snippets else ""
            answer = (
                f"In chapter «{chapter.title}», memory evidence suggests: {excerpt}"
                if excerpt
                else f"No dense evidence for «{qtext}» in this chapter window."
            )
            mids = [pick] if pick in allowed else []
            if not mids:
                continue
            guided.append(
                {
                    "layer": layer,
                    "question_key": qkey,
                    "answer": answer,
                    "memory_ids": mids,
                    "stance": "actual",
                }
            )
            profile.append(
                {
                    "layer": layer,
                    "sentence": answer.rstrip(".") + ".",
                    "memory_ids": mids,
                    "stance": "actual",
                }
            )
            sort_i += 1
    return {
        "journaling_bias_note": _journaling_bias_note(snippets),
        "guided": guided,
        "profile": profile,
    }


def _llm_digest(chapter: ChapterTarget, snippets: list[MemorySnippet]) -> dict[str, Any] | None:
    if not snippets:
        return None
    user = _bundle_for_llm(chapter, snippets)
    user += "\n\nAnswer all guided questions listed in the system schema for layers: values, voice, decision_style, modes."
    raw = chat_json_for_task(GUIDED_DIGEST_SYSTEM, user, temperature=0.25)
    if not raw or not isinstance(raw, dict):
        return None
    return raw


def _sanitize_payload(
    payload: dict[str, Any], allowed: set[str]
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    note = str(payload.get("journaling_bias_note") or "").strip()
    guided_out: list[dict[str, Any]] = []
    for row in payload.get("guided") or []:
        if not isinstance(row, dict):
            continue
        layer = str(row.get("layer") or "")
        if layer not in GUIDED_QUESTIONS:
            continue
        answer = str(row.get("answer") or "").strip()
        mids = _normalize_ids(row.get("memory_ids"), allowed)
        if not answer or not mids:
            continue
        stance = str(row.get("stance") or "actual")
        if stance not in ("actual", "aspiration", "mode_difference"):
            stance = "actual"
        guided_out.append(
            {
                "layer": layer,
                "question_key": str(row.get("question_key") or ""),
                "answer": answer,
                "memory_ids": mids,
                "stance": stance,
            }
        )

    profile_out: list[dict[str, Any]] = []
    for row in payload.get("profile") or []:
        if not isinstance(row, dict):
            continue
        layer = str(row.get("layer") or "")
        if layer not in GUIDED_QUESTIONS:
            continue
        sentence = str(row.get("sentence") or "").strip()
        mids = _normalize_ids(row.get("memory_ids"), allowed)
        if not sentence or not mids:
            continue
        stance = str(row.get("stance") or "actual")
        if stance not in ("actual", "aspiration", "mode_difference"):
            stance = "actual"
        profile_out.append(
            {
                "layer": layer,
                "sentence": sentence,
                "memory_ids": mids,
                "stance": stance,
            }
        )
    return note, guided_out, profile_out


def _existing_digest(conn, chapter_id: UUID, status: DigestStatus) -> UUID | None:
    row = fetch_one(
        conn,
        """
        SELECT id FROM life_chapter_guided_digests
        WHERE chapter_id = %s AND status = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (chapter_id, status),
    )
    return row["id"] if row else None


def _persist_digest(
    conn,
    *,
    subject_id: UUID,
    chapter_id: UUID,
    status: DigestStatus,
    model_id: str | None,
    journaling_bias_note: str,
    guided: list[dict[str, Any]],
    profile: list[dict[str, Any]],
) -> UUID:
    digest_id = uuid7()
    execute(
        conn,
        """
        INSERT INTO life_chapter_guided_digests (
          id, subject_id, chapter_id, status, model_id, prompt_version,
          journaling_bias_note, metadata
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            digest_id,
            subject_id,
            chapter_id,
            status,
            model_id,
            PROMPT_VERSION,
            journaling_bias_note or None,
            json.dumps({"guided_count": len(guided), "profile_sentence_count": len(profile)}),
        ),
    )
    sort_order = 0
    for row in guided:
        item_id = uuid7()
        execute(
            conn,
            """
            INSERT INTO life_chapter_guided_digest_items (
              id, digest_id, step, layer, question_key, text, stance, sort_order
            ) VALUES (%s, %s, 'guided_qa', %s, %s, %s, %s, %s)
            """,
            (
                item_id,
                digest_id,
                row["layer"],
                row.get("question_key") or None,
                row["answer"],
                row.get("stance"),
                sort_order,
            ),
        )
        for mid in row["memory_ids"]:
            execute(
                conn,
                """
                INSERT INTO life_chapter_guided_digest_evidence (
                  id, item_id, memory_id, relationship
                ) VALUES (%s, %s, %s, 'supports')
                """,
                (uuid7(), item_id, UUID(mid)),
            )
        sort_order += 1

    for row in profile:
        item_id = uuid7()
        execute(
            conn,
            """
            INSERT INTO life_chapter_guided_digest_items (
              id, digest_id, step, layer, question_key, text, stance, sort_order
            ) VALUES (%s, %s, 'profile_sentence', %s, NULL, %s, %s, %s)
            """,
            (
                item_id,
                digest_id,
                row["layer"],
                row["sentence"],
                row.get("stance"),
                sort_order,
            ),
        )
        for mid in row["memory_ids"]:
            execute(
                conn,
                """
                INSERT INTO life_chapter_guided_digest_evidence (
                  id, item_id, memory_id, relationship
                ) VALUES (%s, %s, %s, 'supports')
                """,
                (uuid7(), item_id, UUID(mid)),
            )
        sort_order += 1
    return digest_id


def synthesize_chapter_digest(
    auth: AuthContext,
    chapter: ChapterTarget,
    snippets: list[MemorySnippet],
    *,
    use_llm: bool,
) -> dict[str, Any]:
    if len(snippets) < 1:
        raise ValidationError("chapter has no memory evidence for digest")
    allowed = {str(s.memory_id) for s in snippets}
    payload: dict[str, Any] | None = None
    if use_llm:
        payload = _llm_digest(chapter, snippets)
    if not payload:
        payload = _deterministic_digest(chapter, snippets)
    note, guided, profile = _sanitize_payload(payload, allowed)
    if not guided or not profile:
        raise ValidationError("digest synthesis produced no evidence-linked rows")
    if not note:
        note = _journaling_bias_note(snippets)
    return {
        "journaling_bias_note": note,
        "guided": guided,
        "profile": profile,
        "model_id": "ollama_or_llama_server" if use_llm else "deterministic",
    }


def run_chapter_guided_digests(
    auth: AuthContext,
    *,
    dry_run: bool = True,
    use_llm: bool = False,
    chapter_status: str = "proposed",
    digest_status: DigestStatus = "draft",
    replace_existing: bool = False,
    audit_path: Path | None = None,
    max_chapters: int | None = None,
) -> GuidedDigestReport:
    report = GuidedDigestReport(dry_run=dry_run, use_llm=use_llm)
    with transaction() as conn:
        chapters = _load_chapter_targets(conn, auth.subject_id, chapter_status=chapter_status)
    if max_chapters is not None:
        chapters = chapters[:max_chapters]
    report.chapters_seen = len(chapters)

    for chapter in chapters:
        with transaction() as conn:
            if not replace_existing and _existing_digest(conn, chapter.chapter_id, digest_status):
                report.skipped_existing += 1
                continue
            snippets = _load_chapter_memory_snippets(conn, auth.subject_id, chapter.chapter_id)
        if len(snippets) < 1:
            report.skipped_thin += 1
            continue

        try:
            synth = synthesize_chapter_digest(auth, chapter, snippets, use_llm=use_llm)
        except ValidationError:
            report.skipped_thin += 1
            continue

        entry = {
            "chapter_id": str(chapter.chapter_id),
            "title": chapter.title,
            "strand": chapter.strand_slug,
            "memory_evidence_count": len(snippets),
            "guided_answers": len(synth["guided"]),
            "profile_sentences": len(synth["profile"]),
            "journaling_bias_note": synth["journaling_bias_note"],
            "status": digest_status,
        }
        if dry_run:
            report.digests.append(entry)
            continue

        with transaction() as conn:
            if replace_existing:
                old = _existing_digest(conn, chapter.chapter_id, digest_status)
                if old:
                    execute(
                        conn,
                        "DELETE FROM life_chapter_guided_digests WHERE id = %s",
                        (old,),
                    )
            digest_id = _persist_digest(
                conn,
                subject_id=auth.subject_id,
                chapter_id=chapter.chapter_id,
                status=digest_status,
                model_id=synth.get("model_id"),
                journaling_bias_note=synth["journaling_bias_note"],
                guided=synth["guided"],
                profile=synth["profile"],
            )
        entry["digest_id"] = str(digest_id)
        report.digests.append(entry)
        report.digests_written += 1

    if audit_path is not None:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8")
    return report


def load_digest_summary(conn, digest_id: UUID) -> dict[str, Any]:
    row = fetch_one(
        conn,
        "SELECT * FROM life_chapter_guided_digests WHERE id = %s",
        (digest_id,),
    )
    if row is None:
        raise NotFoundError("guided digest not found")
    items = fetch_all(
        conn,
        """
        SELECT i.id, i.step, i.layer, i.text, i.stance,
               array_agg(e.memory_id::text) AS memory_ids
        FROM life_chapter_guided_digest_items i
        LEFT JOIN life_chapter_guided_digest_evidence e ON e.item_id = i.id
        WHERE i.digest_id = %s
        GROUP BY i.id, i.step, i.layer, i.text, i.stance, i.sort_order
        ORDER BY i.sort_order
        """,
        (digest_id,),
    )
    for it in items:
        mids = [m for m in (it.get("memory_ids") or []) if m]
        it["memory_ids"] = mids
        if it["step"] == "profile_sentence" and not mids:
            raise ValidationError("profile sentence missing evidence")
    return {"digest": row, "items": items}
