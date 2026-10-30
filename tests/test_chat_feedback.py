from __future__ import annotations

from virtual_self.auth.credentials import resolve_auth
from virtual_self.config import get_settings
from virtual_self.persistence.db import connection, fetch_one
from virtual_self.persistence.migrate import apply_migrations
from virtual_self.services.chat_feedback import (
    FEEDBACK_REWRITE_QUALITY,
    anti_pattern_candidates_from_reply,
    parse_feedback_command,
    submit_chat_turn_feedback,
)
from virtual_self.services.chat_sessions import append_turn, open_cli_session


def test_parse_feedback_commands():
    assert parse_feedback_command("/good") is not None
    assert parse_feedback_command("/good").rating == "sounds_like_me"
    off = parse_feedback_command("/off — I'd say it straight, no preamble.")
    assert off is not None
    assert off.rating == "off"
    assert "straight" in (off.rewrite or "")
    assert parse_feedback_command("/mode off") is None


def test_anti_pattern_candidates_from_slop_reply():
    bad = (
        "Great question! Here is a thoughtful answer. "
        "I hope this helps — let me know if you'd like more detail."
    )
    rows = anti_pattern_candidates_from_reply(bad, rating="off")
    patterns = {p for p, _k, _s in rows}
    assert any(p.startswith("slop:") for p in patterns)


def test_submit_feedback_off_rewrite_and_exemplar(ensure_db):
    apply_migrations()
    get_settings.cache_clear()
    auth = resolve_auth(f"Bearer {get_settings().bootstrap_token.get_secret_value()}")

    session_id = open_cli_session(auth)
    append_turn(auth, session_id, role="user", content="How do you talk when tired?")
    slop_reply = (
        "Great question! As an AI, I want to acknowledge your feelings. "
        "Here is a structured list:\n\n**Summary**\n- point one\n- point two\n\n"
        "I hope this helps — let me know if you'd like anything else."
    )
    asst = append_turn(auth, session_id, role="assistant", content=slop_reply)
    rewrite = (
        "I'm wiped. I'd keep it short: I'm tired, brain's fuzzy, and I'm not dressing it up "
        "with pep talk or bullet lists — just saying I'm running on fumes tonight."
    )

    out = submit_chat_turn_feedback(
        auth,
        turn_id=asst["turn_id"],
        rating="off",
        rewrite=rewrite,
    )
    assert out["anti_patterns_upserted"] >= 1
    assert out["voice_exemplar_id"] is not None

    with connection() as conn:
        fb = fetch_one(
            conn,
            "SELECT rating, rewrite FROM chat_turn_feedback WHERE turn_id = %s",
            (asst["turn_id"],),
        )
        ap = conn.execute(
            """
            SELECT COUNT(*) AS n FROM anti_patterns
            WHERE subject_id = %s AND origin = 'user_correction'
            """,
            (auth.subject_id,),
        ).fetchone()
        ex = fetch_one(
            conn,
            """
            SELECT status, quality_score, response, feedback_turn_id
            FROM voice_exemplars
            WHERE id = %s
            """,
            (out["voice_exemplar_id"],),
        )
        conn.commit()

    assert fb is not None
    assert fb["rating"] == "off"
    assert fb["rewrite"] == rewrite
    assert ap is not None and ap["n"] >= 1
    assert ex is not None
    assert ex["status"] == "approved"
    assert ex["quality_score"] == FEEDBACK_REWRITE_QUALITY
    assert ex["response"] == rewrite
    assert str(ex["feedback_turn_id"]) == str(asst["turn_id"])


def test_submit_feedback_good_telemetry_only(ensure_db):
    apply_migrations()
    get_settings.cache_clear()
    auth = resolve_auth(f"Bearer {get_settings().bootstrap_token.get_secret_value()}")
    session_id = open_cli_session(auth)
    asst = append_turn(auth, session_id, role="assistant", content="Short ack.")
    out = submit_chat_turn_feedback(
        auth,
        turn_id=asst["turn_id"],
        rating="sounds_like_me",
    )
    assert out["anti_patterns_upserted"] == 0
    assert out["voice_exemplar_id"] is None
