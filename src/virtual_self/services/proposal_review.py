"""Approve / reject / edit life chapters and personality proposals (T-5)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from virtual_self.auth.credentials import AuthContext
from virtual_self.domain.errors import ConflictError, NotFoundError, ValidationError
from virtual_self.persistence.db import fetch_all, fetch_one, transaction

ProposalKind = Literal["life_chapter", "personality"]


def _normalize_ref(ref: str) -> str:
    raw = ref.strip().lower()
    if raw.startswith("p-"):
        raw = raw[2:]
    return raw


def _resolve_uuid_by_prefix(
    conn,
    *,
    table: str,
    subject_col: str,
    subject_id: UUID,
    ref: str,
    extra_where: str = "",
    extra_params: tuple[Any, ...] = (),
) -> UUID | None:
    normalized = _normalize_ref(ref)
    try:
        uid = UUID(normalized)
        row = fetch_one(
            conn,
            f"""
            SELECT id FROM {table}
            WHERE id = %s AND {subject_col} = %s {extra_where}
            LIMIT 1
            """,
            (uid, subject_id, *extra_params),
        )
        return row["id"] if row else None
    except ValueError:
        pass

    pattern = normalized + "%"
    rows = fetch_all(
        conn,
        f"""
        SELECT id FROM {table}
        WHERE {subject_col} = %s AND id::text ILIKE %s {extra_where}
        """,
        (subject_id, pattern, *extra_params),
    )
    if len(rows) == 1:
        return rows[0]["id"]
    if len(rows) > 1:
        raise ValidationError(
            f"ambiguous proposal id prefix: {ref!r}",
            details={"matches": len(rows)},
        )
    return None


def resolve_proposal(
    auth: AuthContext,
    ref: str,
) -> tuple[ProposalKind, UUID]:
    with transaction() as conn:
        chapter_id = _resolve_uuid_by_prefix(
            conn,
            table="life_chapters",
            subject_col="subject_id",
            subject_id=auth.subject_id,
            ref=ref,
            extra_where="AND status = 'proposed'",
        )
        if chapter_id is not None:
            return "life_chapter", chapter_id

        personality_id = _resolve_personality_proposal_id(conn, auth.subject_id, ref)
        if personality_id is not None:
            return "personality", personality_id

    raise NotFoundError(f"no pending proposal matches id {ref!r}")


def _resolve_personality_proposal_id(conn, subject_id: UUID, ref: str) -> UUID | None:
    normalized = _normalize_ref(ref)
    try:
        uid = UUID(normalized)
        row = fetch_one(
            conn,
            """
            SELECT pp.id
            FROM personality_proposals pp
            JOIN personality_profiles prof ON prof.id = pp.profile_id
            WHERE prof.subject_id = %s AND pp.id = %s AND pp.status = 'pending'
            LIMIT 1
            """,
            (subject_id, uid),
        )
        return row["id"] if row else None
    except ValueError:
        pass

    pattern = normalized + "%"
    rows = fetch_all(
        conn,
        """
        SELECT pp.id
        FROM personality_proposals pp
        JOIN personality_profiles prof ON prof.id = pp.profile_id
        WHERE prof.subject_id = %s AND pp.status = 'pending' AND pp.id::text ILIKE %s
        """,
        (subject_id, pattern),
    )
    if len(rows) == 1:
        return rows[0]["id"]
    if len(rows) > 1:
        raise ValidationError(
            f"ambiguous proposal id prefix: {ref!r}",
            details={"matches": len(rows)},
        )
    return None


def _active_profile_id(conn, subject_id: UUID) -> UUID:
    row = fetch_one(
        conn,
        """
        SELECT id FROM personality_profiles
        WHERE subject_id = %s AND status = 'active'
        ORDER BY version DESC
        LIMIT 1
        """,
        (subject_id,),
    )
    if row is None:
        raise NotFoundError("no active personality profile")
    return row["id"]


def approve_life_chapter(auth: AuthContext, chapter_id: UUID) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with transaction() as conn:
        row = fetch_one(
            conn,
            """
            SELECT id, title, status FROM life_chapters
            WHERE id = %s AND subject_id = %s
            FOR UPDATE
            """,
            (chapter_id, auth.subject_id),
        )
        if row is None:
            raise NotFoundError("life chapter not found")
        if row["status"] != "proposed":
            raise ConflictError(
                f"chapter is {row['status']}, not proposed",
                details={"status": row["status"]},
            )
        conn.execute(
            """
            UPDATE life_chapters
            SET status = 'active', approved_at = %s, approved_by = %s, updated_at = %s
            WHERE id = %s
            """,
            (now, auth.actor_id, now, chapter_id),
        )
    return {"kind": "life_chapter", "id": str(chapter_id), "status": "active", "title": row["title"]}


def reject_life_chapter(auth: AuthContext, chapter_id: UUID) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with transaction() as conn:
        row = fetch_one(
            conn,
            """
            SELECT id, title, status FROM life_chapters
            WHERE id = %s AND subject_id = %s
            FOR UPDATE
            """,
            (chapter_id, auth.subject_id),
        )
        if row is None:
            raise NotFoundError("life chapter not found")
        if row["status"] != "proposed":
            raise ConflictError(
                f"chapter is {row['status']}, not proposed",
                details={"status": row["status"]},
            )
        conn.execute(
            """
            UPDATE life_chapters
            SET status = 'rejected', updated_at = %s
            WHERE id = %s
            """,
            (now, chapter_id),
        )
    return {"kind": "life_chapter", "id": str(chapter_id), "status": "rejected", "title": row["title"]}


def edit_life_chapter(
    auth: AuthContext,
    chapter_id: UUID,
    *,
    title: str | None = None,
    summary: str | None = None,
) -> dict[str, Any]:
    if title is None and summary is None:
        raise ValidationError("edit requires title and/or summary")
    if title is not None and not title.strip():
        raise ValidationError("title must be non-empty when provided")
    now = datetime.now(timezone.utc)
    with transaction() as conn:
        row = fetch_one(
            conn,
            """
            SELECT id, title, summary, status FROM life_chapters
            WHERE id = %s AND subject_id = %s
            FOR UPDATE
            """,
            (chapter_id, auth.subject_id),
        )
        if row is None:
            raise NotFoundError("life chapter not found")
        if row["status"] != "proposed":
            raise ConflictError(
                f"chapter is {row['status']}, not proposed",
                details={"status": row["status"]},
            )
        new_title = title.strip() if title is not None else row["title"]
        new_summary = summary if summary is not None else row["summary"]
        conn.execute(
            """
            UPDATE life_chapters
            SET title = %s, summary = %s, updated_at = %s
            WHERE id = %s
            """,
            (new_title, new_summary, now, chapter_id),
        )
    return {
        "kind": "life_chapter",
        "id": str(chapter_id),
        "status": "proposed",
        "title": new_title,
        "summary": new_summary,
    }


def approve_personality_proposal(auth: AuthContext, proposal_id: UUID) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with transaction() as conn:
        profile_id = _active_profile_id(conn, auth.subject_id)
        row = fetch_one(
            conn,
            """
            SELECT id, facet_type, proposed_content, status, origin_mode
            FROM personality_proposals
            WHERE id = %s AND profile_id = %s
            FOR UPDATE
            """,
            (proposal_id, profile_id),
        )
        if row is None:
            raise NotFoundError("personality proposal not found")
        if row["status"] != "pending":
            raise ConflictError(
                f"proposal is {row['status']}, not pending",
                details={"status": row["status"]},
            )
        conn.execute(
            """
            UPDATE personality_proposals
            SET status = 'approved', decided_at = %s, proposed_by = COALESCE(proposed_by, %s)
            WHERE id = %s
            """,
            (now, auth.actor_id, proposal_id),
        )
    result = {
        "kind": "personality",
        "id": str(proposal_id),
        "status": "approved",
        "facet_type": row["facet_type"],
        "origin_mode": row["origin_mode"],
    }
    if row["facet_type"] == "voice":
        from virtual_self.services.voice_cards import apply_approved_voice_proposal

        result["voice_apply"] = apply_approved_voice_proposal(auth, proposal_id)
    elif row["facet_type"] == "traits":
        from virtual_self.services.trait_values_profile import apply_approved_facet_proposal

        result["traits_apply"] = apply_approved_facet_proposal(
            auth, proposal_id, expected_facet_type="traits", priority=75
        )
    elif row["facet_type"] == "values":
        from virtual_self.services.trait_values_profile import apply_approved_facet_proposal

        result["values_apply"] = apply_approved_facet_proposal(
            auth, proposal_id, expected_facet_type="values", priority=90
        )
    elif row["facet_type"] == "interaction_preferences":
        from virtual_self.services.trait_values_profile import apply_approved_facet_proposal

        result["interaction_apply"] = apply_approved_facet_proposal(
            auth, proposal_id, expected_facet_type="interaction_preferences", priority=70
        )
    elif row["facet_type"].startswith("mode_"):
        from virtual_self.services.mode_cards import CANONICAL_MODES, apply_approved_mode_proposal

        mode_key = row["facet_type"][5:]
        mode_name = next((m for m in CANONICAL_MODES if m.lower() == mode_key), None)
        if mode_name is None:
            raise ValidationError(f"unknown mode facet {row['facet_type']!r}")
        result["mode_apply"] = apply_approved_mode_proposal(
            auth, proposal_id, mode=mode_name
        )
    return result


def reject_personality_proposal(auth: AuthContext, proposal_id: UUID) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with transaction() as conn:
        profile_id = _active_profile_id(conn, auth.subject_id)
        row = fetch_one(
            conn,
            """
            SELECT id, facet_type, status, origin_mode
            FROM personality_proposals
            WHERE id = %s AND profile_id = %s
            FOR UPDATE
            """,
            (proposal_id, profile_id),
        )
        if row is None:
            raise NotFoundError("personality proposal not found")
        if row["status"] != "pending":
            raise ConflictError(
                f"proposal is {row['status']}, not pending",
                details={"status": row["status"]},
            )
        conn.execute(
            """
            UPDATE personality_proposals
            SET status = 'rejected', decided_at = %s
            WHERE id = %s
            """,
            (now, proposal_id),
        )
    return {
        "kind": "personality",
        "id": str(proposal_id),
        "status": "rejected",
        "facet_type": row["facet_type"],
        "origin_mode": row["origin_mode"],
    }


def edit_personality_proposal(
    auth: AuthContext,
    proposal_id: UUID,
    *,
    proposed_content: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    if proposed_content is None and reason is None:
        raise ValidationError("edit requires proposed_content and/or reason")
    if proposed_content is not None and not proposed_content.strip():
        raise ValidationError("proposed_content must be non-empty when provided")
    with transaction() as conn:
        profile_id = _active_profile_id(conn, auth.subject_id)
        row = fetch_one(
            conn,
            """
            SELECT id, facet_type, proposed_content, reason, status, origin_mode
            FROM personality_proposals
            WHERE id = %s AND profile_id = %s
            FOR UPDATE
            """,
            (proposal_id, profile_id),
        )
        if row is None:
            raise NotFoundError("personality proposal not found")
        if row["status"] != "pending":
            raise ConflictError(
                f"proposal is {row['status']}, not pending",
                details={"status": row["status"]},
            )
        new_content = proposed_content.strip() if proposed_content is not None else row["proposed_content"]
        new_reason = reason if reason is not None else row["reason"]
        conn.execute(
            """
            UPDATE personality_proposals
            SET proposed_content = %s, reason = %s
            WHERE id = %s
            """,
            (new_content, new_reason, proposal_id),
        )
    return {
        "kind": "personality",
        "id": str(proposal_id),
        "status": "pending",
        "facet_type": row["facet_type"],
        "proposed_content": new_content,
        "origin_mode": row["origin_mode"],
    }


def approve_by_ref(auth: AuthContext, ref: str) -> dict[str, Any]:
    kind, pid = resolve_proposal(auth, ref)
    if kind == "life_chapter":
        return approve_life_chapter(auth, pid)
    return approve_personality_proposal(auth, pid)


def reject_by_ref(auth: AuthContext, ref: str) -> dict[str, Any]:
    kind, pid = resolve_proposal(auth, ref)
    if kind == "life_chapter":
        return reject_life_chapter(auth, pid)
    return reject_personality_proposal(auth, pid)


def edit_by_ref(
    auth: AuthContext,
    ref: str,
    *,
    text: str,
) -> dict[str, Any]:
    kind, pid = resolve_proposal(auth, ref)
    if kind == "life_chapter":
        return edit_life_chapter(auth, pid, title=text.strip())
    return edit_personality_proposal(auth, pid, proposed_content=text.strip())
