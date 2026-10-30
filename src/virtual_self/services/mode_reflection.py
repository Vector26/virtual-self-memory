"""Per-mode chat reflection → pending personality proposals (E-1 / persona.reflect)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID

from uuid6 import uuid7

from virtual_self.auth.credentials import AuthContext
from virtual_self.config import get_settings
from virtual_self.persistence.db import fetch_all, fetch_one, transaction
from virtual_self.services import jobs
from virtual_self.services.mode_cards import (
    CANONICAL_MODES,
    ModeName,
    _MODE_DEFAULTS,
    _MODE_MATCHERS,
)
from virtual_self.services.trait_values_profile import _active_profile_id

ReflectionLens = ModeName | Literal["Self"]
REFLECTION_FACET = "reflection"
REFLECTION_REASON = (
    "E-1 per-mode reflection on recent chat — pending only; approve or reject via T-5."
)

_USER_WORD = re.compile(r"[a-z0-9']+", re.I)


@dataclass(frozen=True)
class ChatTurnRow:
    turn_id: UUID
    role: str
    content: str
    evidence_record_id: UUID | None
    session_id: UUID
    session_active_mode: str | None


def set_session_active_mode(
    auth: AuthContext,
    session_id: UUID,
    active_mode: ModeName | None,
) -> None:
    """Persist manual /mode state on chat_sessions.metadata for reflection scoping."""
    payload = {"active_mode": active_mode} if active_mode else {"active_mode": None}
    with transaction() as conn:
        row = fetch_one(
            conn,
            "SELECT id FROM chat_sessions WHERE id = %s AND subject_id = %s",
            (session_id, auth.subject_id),
        )
        if row is None:
            return
        conn.execute(
            """
            UPDATE chat_sessions
            SET metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb,
                last_activity_at = NOW()
            WHERE id = %s
            """,
            (json.dumps(payload), session_id),
        )


def _content_fingerprint(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def _normalize_mode_token(raw: str | None) -> ReflectionLens | None:
    if not raw:
        return None
    key = raw.strip().lower()
    if key in ("self", "integrated", "present"):
        return "Self"
    for m in CANONICAL_MODES:
        if m.lower() == key:
            return m
    return None


def _load_active_reflection_lenses(conn, subject_id: UUID) -> list[ReflectionLens]:
    rows = fetch_all(
        conn,
        """
        SELECT name, status FROM persona_modes
        WHERE subject_id = %s AND name = ANY(%s)
        """,
        (subject_id, list(CANONICAL_MODES)),
    )
    by_name = {str(r["name"]): str(r["status"]) for r in rows}
    lenses: list[ReflectionLens] = ["Self"]
    for mode in CANONICAL_MODES:
        status = by_name.get(mode) or str(_MODE_DEFAULTS[mode]["status"])
        if status == "active":
            lenses.append(mode)
    return lenses


def _fetch_recent_turns(conn, subject_id: UUID, *, limit: int, since: datetime) -> list[ChatTurnRow]:
    rows = fetch_all(
        conn,
        """
        SELECT
          ct.id AS turn_id,
          ct.role,
          ct.content,
          ct.evidence_record_id,
          ct.session_id,
          cs.metadata->>'active_mode' AS session_active_mode
        FROM chat_turns ct
        JOIN chat_sessions cs ON cs.id = ct.session_id
        WHERE cs.subject_id = %s AND ct.created_at >= %s
        ORDER BY ct.created_at DESC
        LIMIT %s
        """,
        (subject_id, since, limit),
    )
    out: list[ChatTurnRow] = []
    for r in rows:
        out.append(
            ChatTurnRow(
                turn_id=r["turn_id"],
                role=str(r["role"]),
                content=str(r["content"]),
                evidence_record_id=r.get("evidence_record_id"),
                session_id=r["session_id"],
                session_active_mode=r.get("session_active_mode"),
            )
        )
    return out


def _turns_for_lens(turns: list[ChatTurnRow], lens: ReflectionLens) -> list[ChatTurnRow]:
    if not turns:
        return []
    session_modes = {_normalize_mode_token(t.session_active_mode) for t in turns}
    has_mode_tags = any(m is not None and m != "Self" for m in session_modes)

    if lens == "Self":
        if has_mode_tags:
            picked = [
                t
                for t in turns
                if _normalize_mode_token(t.session_active_mode) in (None, "Self")
            ]
        else:
            picked = list(turns)
    else:
        picked = [
            t
            for t in turns
            if _normalize_mode_token(t.session_active_mode) == lens
        ]
        if not picked and not has_mode_tags:
            pat = _MODE_MATCHERS.get(lens)
            if pat:
                picked = [t for t in turns if pat.search(t.content)]
            else:
                picked = []

    user_turns = [t for t in picked if t.role == "user"]
    if len(user_turns) < 1:
        return []
    return picked[: min(len(picked), 24)]


def _top_tokens(text: str, *, limit: int = 6) -> list[str]:
    stop = {
        "the",
        "and",
        "that",
        "this",
        "with",
        "have",
        "from",
        "your",
        "about",
        "just",
        "what",
        "when",
        "they",
        "would",
        "there",
        "been",
        "into",
        "mode",
    }
    counts: dict[str, int] = {}
    for m in _USER_WORD.finditer(text.lower()):
        w = m.group(0)
        if len(w) < 3 or w in stop:
            continue
        counts[w] = counts.get(w, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [w for w, _ in ranked[:limit]]


def _synthesize_insight(lens: ReflectionLens, turns: list[ChatTurnRow]) -> str | None:
    user_blob = " ".join(t.content for t in turns if t.role == "user").strip()
    if len(user_blob) < 12:
        return None
    themes = _top_tokens(user_blob)
    theme_bit = ", ".join(themes[:4]) if themes else "recent chat themes"
    n_user = sum(1 for t in turns if t.role == "user")

    if lens == "Self":
        return (
            f"Integrated self read on {n_user} recent user turns: recurring focus on "
            f"{theme_bit}; worth checking whether this still matches how you want to show up."
        )
    defaults = _MODE_DEFAULTS[lens]
    emphasis = str(defaults["values_delta"].get("emphasis", lens))
    register = str(defaults["voice_delta"].get("register", lens))
    return (
        f"Through {lens}'s lens ({register}): in {n_user} recent turns you keep circling "
        f"{theme_bit} — aligns with {emphasis}; consider whether to lean in or rebalance."
    )


def expire_stale_reflection_proposals(
    conn,
    profile_id: UUID,
    *,
    stale_days: int,
) -> int:
    row = fetch_one(
        conn,
        """
        WITH updated AS (
          UPDATE personality_proposals
          SET status = 'expired', decided_at = NOW()
          WHERE profile_id = %s
            AND facet_type = %s
            AND status = 'pending'
            AND created_at < NOW() - (%s || ' days')::interval
          RETURNING id
        )
        SELECT COUNT(*) AS n FROM updated
        """,
        (profile_id, REFLECTION_FACET, str(stale_days)),
    )
    return int(row["n"]) if row else 0


def _pending_reflection_for_mode(conn, profile_id: UUID, lens: ReflectionLens) -> bool:
    origin = lens if lens != "Self" else "Self"
    row = fetch_one(
        conn,
        """
        SELECT id FROM personality_proposals
        WHERE profile_id = %s AND facet_type = %s AND status = 'pending'
          AND COALESCE(origin_mode, 'Self') = %s
        LIMIT 1
        """,
        (profile_id, REFLECTION_FACET, origin),
    )
    return row is not None


def _duplicate_content_pending(conn, profile_id: UUID, fingerprint: str) -> bool:
    rows = fetch_all(
        conn,
        """
        SELECT proposed_content FROM personality_proposals
        WHERE profile_id = %s AND facet_type = %s AND status = 'pending'
        """,
        (profile_id, REFLECTION_FACET),
    )
    for r in rows:
        if _content_fingerprint(str(r["proposed_content"])) == fingerprint:
            return True
    return False


def _insert_reflection_proposal(
    conn,
    *,
    profile_id: UUID,
    lens: ReflectionLens,
    proposed_content: str,
    actor_id: UUID,
    turn_rows: list[ChatTurnRow],
    confidence: float,
) -> UUID:
    origin = lens if lens != "Self" else "Self"
    proposal_id = uuid7()
    conn.execute(
        """
        INSERT INTO personality_proposals (
          id, profile_id, facet_type, proposed_content, reason,
          confidence, proposed_by, origin_mode, status
        ) VALUES (
          %s, %s, %s, %s, %s,
          %s, %s, %s, 'pending'
        )
        """,
        (
            proposal_id,
            profile_id,
            REFLECTION_FACET,
            proposed_content,
            REFLECTION_REASON,
            confidence,
            actor_id,
            origin,
        ),
    )
    seen_evidence: set[UUID] = set()
    for t in turn_rows:
        if t.evidence_record_id is None or t.evidence_record_id in seen_evidence:
            continue
        seen_evidence.add(t.evidence_record_id)
        conn.execute(
            """
            INSERT INTO personality_proposal_evidence (
              id, proposal_id, evidence_record_id, relationship
            ) VALUES (%s, %s, %s, 'illustrates')
            """,
            (uuid7(), proposal_id, t.evidence_record_id),
        )
    return proposal_id


def format_reflection_digest_line(
    *,
    proposal_id: UUID,
    lens: ReflectionLens,
    proposed_content: str,
) -> str:
    short = str(proposal_id).split("-", 1)[0]
    snippet = proposed_content.strip().replace("\n", " ")[:140]
    label = lens if lens != "Self" else "Self"
    return (
        f"[{label}] proposal {short}: reflection — {snippet}. "
        f"/approve {short} · /reject {short} · /edit {short} <revised text>"
    )


def notify_reflection_digest(
    entries: list[tuple[UUID, ReflectionLens, str]],
) -> tuple[bool, str]:
    """Single configured notifier digest; each line carries a [Mode] prefix."""
    from virtual_self.observability.notify import NotifyCategory, NotifySeverity, notify_user

    if not entries:
        return False, ""
    lines = [
        format_reflection_digest_line(
            proposal_id=pid, lens=lens, proposed_content=content
        )
        for pid, lens, content in entries
    ]
    body = f"Reflection digest ({len(lines)} pending):\n" + "\n".join(lines)
    ok = notify_user(
        body,
        category=NotifyCategory.DEFAULT,
        severity=NotifySeverity.USER_ACTIONABLE,
        source_label="Self",
    )
    return ok, body


def run_mode_reflection(
    auth: AuthContext,
    *,
    dry_run: bool = False,
    notify: bool = True,
) -> dict[str, Any]:
    settings = get_settings()
    since = datetime.now(timezone.utc) - timedelta(days=settings.persona_reflect_lookback_days)
    created: dict[str, str] = {}
    skipped: dict[str, str] = {}
    expired = 0
    digest_entries: list[tuple[UUID, ReflectionLens, str]] = []
    lenses: list[ReflectionLens] = []

    with transaction() as conn:
        profile_id = _active_profile_id(conn, auth.subject_id)
        expired = expire_stale_reflection_proposals(
            conn, profile_id, stale_days=settings.persona_reflect_stale_days
        )
        lenses = _load_active_reflection_lenses(conn, auth.subject_id)
        turns = _fetch_recent_turns(
            conn,
            auth.subject_id,
            limit=settings.persona_reflect_turn_window,
            since=since,
        )

        for lens in lenses:
            scoped = _turns_for_lens(turns, lens)
            if len([t for t in scoped if t.role == "user"]) < settings.persona_reflect_min_user_turns:
                skipped[lens] = "insufficient_user_turns"
                continue
            insight = _synthesize_insight(lens, scoped)
            if not insight:
                skipped[lens] = "no_insight"
                continue
            fp = _content_fingerprint(insight)
            if _pending_reflection_for_mode(conn, profile_id, lens):
                skipped[lens] = "pending_exists"
                continue
            if _duplicate_content_pending(conn, profile_id, fp):
                skipped[lens] = "duplicate_content"
                continue
            if dry_run:
                created[lens] = "dry-run"
                digest_entries.append((uuid7(), lens, insight))
                continue
            pid = _insert_reflection_proposal(
                conn,
                profile_id=profile_id,
                lens=lens,
                proposed_content=insight,
                actor_id=auth.actor_id,
                turn_rows=scoped,
                confidence=0.72,
            )
            created[lens] = str(pid)
            digest_entries.append((pid, lens, insight))

    notified = False
    digest_body = ""
    if notify and not dry_run and digest_entries:
        notified, digest_body = notify_reflection_digest(digest_entries)

    return {
        "dry_run": dry_run,
        "expired_stale": expired,
        "proposals": created,
        "skipped": skipped,
        "notified": notified,
        "digest_preview": digest_body[:500] if digest_body else "",
        "lenses": lenses,
    }


def _last_completed_reflect_at(conn, subject_id: UUID) -> datetime | None:
    row = fetch_one(
        conn,
        """
        SELECT updated_at FROM jobs
        WHERE job_type = 'persona.reflect'
          AND state = 'completed'
          AND payload_ref->>'subject_id' = %s
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (str(subject_id),),
    )
    if row and row.get("updated_at"):
        ts = row["updated_at"]
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts
    return None


def _count_user_turns_since(conn, subject_id: UUID, since: datetime | None) -> int:
    if since is None:
        row = fetch_one(
            conn,
            """
            SELECT COUNT(*) AS n
            FROM chat_turns ct
            JOIN chat_sessions cs ON cs.id = ct.session_id
            WHERE cs.subject_id = %s AND ct.role = 'user'
            """,
            (subject_id,),
        )
    else:
        row = fetch_one(
            conn,
            """
            SELECT COUNT(*) AS n
            FROM chat_turns ct
            JOIN chat_sessions cs ON cs.id = ct.session_id
            WHERE cs.subject_id = %s AND ct.role = 'user' AND ct.created_at > %s
            """,
            (subject_id, since),
        )
    return int(row["n"]) if row else 0


def reflect_schedule_due(conn, subject_id: UUID) -> tuple[bool, str]:
    settings = get_settings()
    if not settings.persona_reflect_enabled:
        return False, "disabled"
    last = _last_completed_reflect_at(conn, subject_id)
    interval = timedelta(hours=settings.persona_reflect_interval_hours)
    if last is None or datetime.now(timezone.utc) - last >= interval:
        return True, "interval"
    new_turns = _count_user_turns_since(conn, subject_id, last)
    if new_turns >= settings.persona_reflect_turn_threshold:
        return True, "turn_threshold"
    return False, "not_due"


def enqueue_persona_reflect(
    auth: AuthContext,
    *,
    reason: str = "scheduled",
    force: bool = False,
) -> UUID | None:
    settings = get_settings()
    if not settings.persona_reflect_enabled:
        return None
    with transaction() as conn:
        if not force:
            due, _why = reflect_schedule_due(conn, auth.subject_id)
            if not due:
                return None
        return jobs.enqueue(
            conn,
            "persona.reflect",
            {
                "subject_id": str(auth.subject_id),
                "actor_id": str(auth.actor_id),
                "reason": reason,
            },
            dedupe_key=f"persona.reflect:{auth.subject_id}",
        )


def maybe_enqueue_persona_reflect_after_turn(auth: AuthContext) -> None:
    enqueue_persona_reflect(auth, reason="turn_threshold")


def run_persona_reflect_job(auth: AuthContext, payload: dict[str, Any]) -> dict[str, Any]:
    notify = bool(payload.get("notify", True))
    return run_mode_reflection(auth, dry_run=False, notify=notify)


def worker_tick_persona_reflect(auth: AuthContext) -> UUID | None:
    """Called from worker idle loop to enqueue periodic reflection."""
    return enqueue_persona_reflect(auth, reason="worker_tick")
