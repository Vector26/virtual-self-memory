"""Chat turn feedback (E-2): corrections → anti_patterns + voice_exemplars."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from uuid6 import uuid7

from virtual_self.auth.credentials import AuthContext
from virtual_self.domain.errors import NotFoundError, ValidationError
from virtual_self.eval.slop import check_slop
from virtual_self.persistence.db import execute, fetch_one, transaction
from virtual_self.providers.embeddings import embed_texts, vector_literal
from virtual_self.services.voice_exemplars import _topic_tags

FeedbackRating = Literal["sounds_like_me", "off", "wrong_fact"]

_GOOD_CMD = re.compile(r"^/good\s*$", re.I)
_OFF_CMD = re.compile(r"^/off(?:\s*[—\-]\s*)?(?P<rest>.*)$", re.I | re.DOTALL)
_WRONG_FACT_CMD = re.compile(
    r"^/wrong(?:[\s_-]?fact)?(?:\s*[—\-]\s*)?(?P<rest>.*)$",
    re.I | re.DOTALL,
)

FEEDBACK_REWRITE_QUALITY = 1.0
_DEFAULT_REGISTER = "casual_chat"


@dataclass(frozen=True)
class FeedbackCommand:
    rating: FeedbackRating
    rewrite: str | None = None
    note: str | None = None


def parse_feedback_command(user_text: str) -> FeedbackCommand | None:
    raw = user_text.strip()
    if not raw.startswith("/"):
        return None
    if _GOOD_CMD.match(raw):
        return FeedbackCommand("sounds_like_me")
    off = _OFF_CMD.match(raw)
    if off:
        rest = (off.group("rest") or "").strip()
        return FeedbackCommand("off", rewrite=rest or None)
    wrong = _WRONG_FACT_CMD.match(raw)
    if wrong:
        rest = (wrong.group("rest") or "").strip()
        return FeedbackCommand("wrong_fact", rewrite=rest or None)
    return None


def feedback_command_ack(cmd: FeedbackCommand) -> str:
    if cmd.rating == "sounds_like_me":
        return "Noted — that reply sounded like you (voice feedback saved)."
    if cmd.rating == "off":
        if cmd.rewrite:
            return "Noted — marked off; your rewrite is saved as a top-priority voice exemplar."
        return "Noted — marked off; slop cues from that reply were added as anti-patterns."
    if cmd.rewrite:
        return "Noted — wrong fact; rewrite saved as exemplar and reply flagged for anti-patterns."
    return "Noted — wrong fact on that reply; anti-patterns updated."


def anti_pattern_candidates_from_reply(
    assistant_content: str,
    *,
    rating: FeedbackRating,
    note: str | None = None,
) -> list[tuple[str, str, int]]:
    """Return (pattern, kind, severity) rows for user_correction origin."""
    text = (assistant_content or "").strip()
    out: list[tuple[str, str, int]] = []
    base_sev = 4 if rating == "wrong_fact" else 3

    slop = check_slop(text[:4000], register=_DEFAULT_REGISTER)
    for flag in slop.flags:
        out.append((f"slop:{flag}", "structure", base_sev))

    note_text = (note or "").strip()
    if note_text and len(note_text) >= 4:
        out.append((note_text[:500], "phrase", min(5, base_sev + 1)))

    if not out and text:
        snippet = " ".join(text.split())[:240]
        if len(snippet) >= 12:
            out.append((snippet, "phrase", base_sev))

    deduped: dict[tuple[str, str], int] = {}
    for pattern, kind, severity in out:
        key = (pattern, kind)
        deduped[key] = max(deduped.get(key, 0), severity)
    return [(p, k, s) for (p, k), s in deduped.items()]


def _load_assistant_turn(conn, auth: AuthContext, turn_id: UUID) -> dict[str, Any]:
    row = fetch_one(
        conn,
        """
        SELECT ct.id, ct.session_id, ct.role, ct.content, ct.turn_index
        FROM chat_turns ct
        JOIN chat_sessions cs ON cs.id = ct.session_id
        WHERE ct.id = %s AND cs.subject_id = %s
        """,
        (turn_id, auth.subject_id),
    )
    if row is None:
        raise NotFoundError("chat turn not found")
    if row["role"] != "assistant":
        raise ValidationError("feedback applies to the assistant reply turn only")
    return row


def _preceding_user_prompt(conn, session_id: UUID, turn_index: int) -> dict[str, Any]:
    row = fetch_one(
        conn,
        """
        SELECT content
        FROM chat_turns
        WHERE session_id = %s AND role = 'user' AND turn_index < %s
        ORDER BY turn_index DESC
        LIMIT 1
        """,
        (session_id, turn_index),
    )
    if row and row.get("content"):
        return {
            "text": str(row["content"]).strip()[:4000],
            "context_synthetic": False,
            "source": "chat_user_turn",
        }
    return {
        "text": "What would you say here?",
        "context_synthetic": True,
        "source": "feedback_missing_user_turn",
    }


def _upsert_anti_patterns(
    conn,
    auth: AuthContext,
    patterns: list[tuple[str, str, int]],
) -> int:
    written = 0
    for pattern, kind, severity in patterns:
        execute(
            conn,
            """
            INSERT INTO anti_patterns (id, subject_id, pattern, kind, origin, severity)
            VALUES (%s, %s, %s, %s, 'user_correction', %s)
            ON CONFLICT (subject_id, pattern, kind) DO UPDATE SET
              severity = GREATEST(anti_patterns.severity, EXCLUDED.severity),
              origin = 'user_correction'
            """,
            (uuid7(), auth.subject_id, pattern, kind, severity),
        )
        written += 1
    return written


def _upsert_rewrite_exemplar(
    conn,
    auth: AuthContext,
    *,
    turn_id: UUID,
    rewrite: str,
    context: dict[str, Any],
) -> UUID:
    rewrite = rewrite.strip()
    if not rewrite:
        raise ValidationError("rewrite must be non-empty")
    exemplar_id = uuid7()
    execute(
        conn,
        """
        DELETE FROM voice_exemplars
        WHERE subject_id = %s AND feedback_turn_id = %s
        """,
        (auth.subject_id, turn_id),
    )
    tags = _topic_tags(rewrite)
    emb_rows = embed_texts([rewrite[:8000]])
    emb = emb_rows[0] if emb_rows else None
    if emb:
        execute(
            conn,
            """
            INSERT INTO voice_exemplars (
              id, subject_id, context, response, register, topic_tags,
              quality_score, status, embedding, feedback_turn_id
            ) VALUES (
              %s, %s, %s::jsonb, %s, %s, %s,
              %s, 'approved', %s::vector, %s
            )
            """,
            (
                exemplar_id,
                auth.subject_id,
                json.dumps(context),
                rewrite,
                _DEFAULT_REGISTER,
                tags,
                FEEDBACK_REWRITE_QUALITY,
                vector_literal(emb),
                turn_id,
            ),
        )
    else:
        execute(
            conn,
            """
            INSERT INTO voice_exemplars (
              id, subject_id, context, response, register, topic_tags,
              quality_score, status, feedback_turn_id
            ) VALUES (
              %s, %s, %s::jsonb, %s, %s, %s,
              %s, 'approved', %s
            )
            """,
            (
                exemplar_id,
                auth.subject_id,
                json.dumps(context),
                rewrite,
                _DEFAULT_REGISTER,
                tags,
                FEEDBACK_REWRITE_QUALITY,
                turn_id,
            ),
        )
    return exemplar_id


def submit_chat_turn_feedback(
    auth: AuthContext,
    *,
    turn_id: UUID,
    rating: FeedbackRating,
    note: str | None = None,
    rewrite: str | None = None,
) -> dict[str, Any]:
    if rating not in ("sounds_like_me", "off", "wrong_fact"):
        raise ValidationError(f"invalid rating {rating!r}")

    rewrite_text = (rewrite or "").strip() or None
    note_text = (note or "").strip() or None

    with transaction() as conn:
        assistant = _load_assistant_turn(conn, auth, turn_id)
        execute(
            conn,
            """
            INSERT INTO chat_turn_feedback (id, turn_id, rating, note, rewrite)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (turn_id) DO UPDATE SET
              rating = EXCLUDED.rating,
              note = EXCLUDED.note,
              rewrite = EXCLUDED.rewrite
            """,
            (uuid7(), turn_id, rating, note_text, rewrite_text),
        )
        fb_row = fetch_one(conn, "SELECT id FROM chat_turn_feedback WHERE turn_id = %s", (turn_id,))
        if fb_row is None:
            raise NotFoundError("chat turn feedback row missing after upsert")
        feedback_id = fb_row["id"]

        anti_written = 0
        exemplar_id: UUID | None = None
        if rating in ("off", "wrong_fact"):
            patterns = anti_pattern_candidates_from_reply(
                str(assistant["content"]),
                rating=rating,
                note=note_text,
            )
            anti_written = _upsert_anti_patterns(conn, auth, patterns)

        if rewrite_text:
            ctx = _preceding_user_prompt(
                conn,
                assistant["session_id"],
                int(assistant["turn_index"]),
            )
            exemplar_id = _upsert_rewrite_exemplar(
                conn,
                auth,
                turn_id=turn_id,
                rewrite=rewrite_text,
                context=ctx,
            )

    return {
        "feedback_id": str(feedback_id),
        "turn_id": str(turn_id),
        "rating": rating,
        "anti_patterns_upserted": anti_written,
        "voice_exemplar_id": str(exemplar_id) if exemplar_id else None,
    }
