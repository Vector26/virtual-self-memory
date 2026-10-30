"""Lineage-aware removal of authored_text rows (E-3 / VSELF-66)."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from virtual_self.auth.credentials import AuthContext
from virtual_self.domain.errors import NotFoundError
from virtual_self.persistence.db import execute, fetch_one, transaction
from virtual_self.services import audit, suppression

AUTHORED_TEXT_SUPPRESSION_KIND = "authored_text"
SUPPRESSED_EXCLUDED_REASON = "user_suppressed"


def _cascade_lineage_effects(
    conn,
    *,
    subject_id: UUID,
    authored_text_id: UUID,
    register: str,
) -> dict[str, int]:
    exemplars_removed = execute(
        conn,
        "DELETE FROM voice_exemplars WHERE subject_id = %s AND authored_text_id = %s",
        (subject_id, authored_text_id),
    )
    style_profiles_flagged = execute(
        conn,
        """
        UPDATE style_profiles
        SET recompute_pending = true
        WHERE subject_id = %s AND register = %s AND recompute_pending = false
        """,
        (subject_id, register),
    )
    facet_versions_flagged = execute(
        conn,
        """
        UPDATE personality_facet_versions fv
        SET needs_review = true
        WHERE fv.needs_review = false
          AND fv.id IN (
            SELECT DISTINCT fe.facet_version_id
            FROM personality_facet_evidence fe
            WHERE fe.authored_text_id = %s
          )
        """,
        (authored_text_id,),
    )
    proposals_flagged = execute(
        conn,
        """
        UPDATE personality_proposals pp
        SET needs_review = true
        WHERE pp.needs_review = false
          AND pp.id IN (
            SELECT DISTINCT pe.proposal_id
            FROM personality_proposal_evidence pe
            WHERE pe.authored_text_id = %s
          )
        """,
        (authored_text_id,),
    )
    return {
        "voice_exemplars_removed": exemplars_removed,
        "style_profiles_flagged": style_profiles_flagged,
        "facet_versions_flagged": facet_versions_flagged,
        "proposals_flagged": proposals_flagged,
    }


def _load_authored_text(conn, subject_id: UUID, authored_text_id: UUID) -> dict[str, Any]:
    row = fetch_one(
        conn,
        """
        SELECT id, subject_id, content, register, text_hash
        FROM authored_texts
        WHERE id = %s AND subject_id = %s
        """,
        (authored_text_id, subject_id),
    )
    if row is None:
        raise NotFoundError("authored_text not found")
    return row


def remove_authored_text_lineage(
    auth: AuthContext,
    authored_text_id: UUID,
    *,
    mode: Literal["delete", "suppress"] = "delete",
    request_id: str | None = None,
) -> dict[str, Any]:
    """Delete or suppress one authored_text and cascade dependent persona artifacts."""
    with transaction() as conn:
        row = _load_authored_text(conn, auth.subject_id, authored_text_id)
        stats = _cascade_lineage_effects(
            conn,
            subject_id=auth.subject_id,
            authored_text_id=authored_text_id,
            register=str(row["register"]),
        )
        suppression.add_suppression(
            conn,
            auth.subject_id,
            AUTHORED_TEXT_SUPPRESSION_KIND,
            str(row["content"]),
            auth.actor_id,
            reason="authored_text_removed" if mode == "delete" else "authored_text_suppressed",
        )
        if mode == "suppress":
            execute(
                conn,
                """
                UPDATE authored_texts
                SET excluded_reason = %s, invalidated_at = NOW()
                WHERE id = %s AND subject_id = %s
                """,
                (SUPPRESSED_EXCLUDED_REASON, authored_text_id, auth.subject_id),
            )
        else:
            execute(
                conn,
                "DELETE FROM authored_texts WHERE id = %s AND subject_id = %s",
                (authored_text_id, auth.subject_id),
            )
        audit.log_event(
            conn,
            request_id=request_id,
            actor_id=auth.actor_id,
            operation=f"authored_text.{mode}",
            target_type="authored_text",
            target_id=authored_text_id,
            metadata={"mode": mode, **stats},
        )
    return {
        "authored_text_id": str(authored_text_id),
        "mode": mode,
        **stats,
    }
