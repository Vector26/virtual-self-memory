from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from uuid6 import uuid7

from virtual_self.auth.credentials import resolve_auth
from virtual_self.config import get_settings
from virtual_self.persistence.db import connection, execute, fetch_all, fetch_one, transaction
from virtual_self.persistence.migrate import apply_migrations
from virtual_self.services.chat_sessions import append_turn, open_cli_session
from virtual_self.services.mode_reflection import (
    REFLECTION_FACET,
    enqueue_persona_reflect,
    expire_stale_reflection_proposals,
    format_reflection_digest_line,
    reflect_schedule_due,
    run_mode_reflection,
    set_session_active_mode,
)
from virtual_self.worker.runner import handle_job


def _ensure_active_profile(conn, subject_id: UUID) -> None:
    row = fetch_one(
        conn,
        "SELECT id FROM personality_profiles WHERE subject_id = %s AND status = 'active' LIMIT 1",
        (subject_id,),
    )
    if row:
        return
    pid = uuid7()
    execute(
        conn,
        """
        INSERT INTO personality_profiles (id, subject_id, name, status, activated_at)
        VALUES (%s, %s, 'test', 'active', NOW())
        """,
        (pid, subject_id),
    )


def _cleanup_chat_sessions(conn, subject_id: UUID) -> None:
    conn.execute("DELETE FROM chat_sessions WHERE subject_id = %s", (subject_id,))


def _cleanup_reflection_proposals(conn, subject_id: UUID) -> None:
    profile = fetch_one(
        conn,
        "SELECT id FROM personality_profiles WHERE subject_id = %s LIMIT 1",
        (subject_id,),
    )
    if not profile:
        return
    conn.execute(
        """
        DELETE FROM personality_proposal_evidence
        WHERE proposal_id IN (
          SELECT id FROM personality_proposals
          WHERE profile_id = %s AND facet_type = %s
        )
        """,
        (profile["id"], REFLECTION_FACET),
    )
    conn.execute(
        """
        DELETE FROM personality_proposals
        WHERE profile_id = %s AND facet_type = %s
        """,
        (profile["id"], REFLECTION_FACET),
    )


def _seed_chat_turns(auth, *, mode: str | None, lines: list[str]) -> None:
    session_id = open_cli_session(auth)
    if mode:
        set_session_active_mode(auth, session_id, mode)  # type: ignore[arg-type]
    for i, line in enumerate(lines):
        role = "user" if i % 2 == 0 else "assistant"
        append_turn(auth, session_id, role=role, content=line)


def test_reflection_digest_line_has_mode_prefix():
    line = format_reflection_digest_line(
        proposal_id=uuid7(),
        lens="Analyst",
        proposed_content="Fictional Analyst insight about narrowing a demo migration.",
    )
    assert line.startswith("[Analyst]")


def test_run_mode_reflection_creates_pending_with_origin_mode(ensure_db, monkeypatch):
    apply_migrations()
    get_settings.cache_clear()
    auth = resolve_auth(f"Bearer {get_settings().bootstrap_token.get_secret_value()}")

    with connection() as conn:
        _ensure_active_profile(conn, auth.subject_id)
        _cleanup_chat_sessions(conn, auth.subject_id)
        _cleanup_reflection_proposals(conn, auth.subject_id)
        conn.commit()

    _seed_chat_turns(
        auth,
        mode="Analyst",
        lines=[
            "Analyst mode — compare the two synthetic migration plans.",
            "Understood — evidence first.",
            "The demo stack still has one failing queue.",
            "Keep the experiment bounded.",
        ],
    )

    notified: list[str] = []

    def _capture_notify(body, **kwargs):
        notified.append(body)
        return True

    monkeypatch.setattr(
        "virtual_self.observability.notify.notify_user",
        _capture_notify,
    )

    out = run_mode_reflection(auth, dry_run=False, notify=True)
    assert out["proposals"]
    assert "Analyst" in out["proposals"]

    with transaction() as conn:
        profile_id = fetch_one(
            conn,
            "SELECT id FROM personality_profiles WHERE subject_id = %s LIMIT 1",
            (auth.subject_id,),
        )["id"]
        rows = fetch_all(
            conn,
            """
            SELECT origin_mode, status, facet_type
            FROM personality_proposals
            WHERE profile_id = %s AND facet_type = %s
            """,
            (profile_id, REFLECTION_FACET),
        )
    assert rows
    assert all(r["status"] == "pending" for r in rows)
    assert all(r["facet_type"] == REFLECTION_FACET for r in rows)
    assert any(r["origin_mode"] == "Analyst" for r in rows)

    assert notified
    assert "[Analyst]" in notified[0]

    with connection() as conn:
        _cleanup_chat_sessions(conn, auth.subject_id)
        _cleanup_reflection_proposals(conn, auth.subject_id)
        conn.commit()


def test_pending_dedupe_skips_second_run(ensure_db, monkeypatch):
    apply_migrations()
    get_settings.cache_clear()
    auth = resolve_auth(f"Bearer {get_settings().bootstrap_token.get_secret_value()}")

    with connection() as conn:
        _ensure_active_profile(conn, auth.subject_id)
        _cleanup_chat_sessions(conn, auth.subject_id)
        _cleanup_reflection_proposals(conn, auth.subject_id)
        conn.commit()

    _seed_chat_turns(
        auth,
        mode=None,
        lines=["Self integrated chat about journaling habits.", "Noted.", "More journal rhythm."],
    )
    monkeypatch.setattr(
        "virtual_self.observability.notify.notify_user",
        lambda *a, **k: True,
    )
    first = run_mode_reflection(auth, dry_run=False, notify=False)
    second = run_mode_reflection(auth, dry_run=False, notify=False)
    assert first["proposals"]
    assert second["skipped"]

    with connection() as conn:
        _cleanup_chat_sessions(conn, auth.subject_id)
        _cleanup_reflection_proposals(conn, auth.subject_id)
        conn.commit()


def test_expire_stale_reflection_proposals(ensure_db):
    apply_migrations()
    get_settings.cache_clear()
    auth = resolve_auth(f"Bearer {get_settings().bootstrap_token.get_secret_value()}")

    with connection() as conn:
        _ensure_active_profile(conn, auth.subject_id)
        _cleanup_reflection_proposals(conn, auth.subject_id)
        profile_id = fetch_one(
            conn,
            "SELECT id FROM personality_profiles WHERE subject_id = %s",
            (auth.subject_id,),
        )["id"]
        pid = uuid7()
        old = datetime.now(timezone.utc) - timedelta(days=45)
        execute(
            conn,
            """
            INSERT INTO personality_proposals (
              id, profile_id, facet_type, proposed_content, reason,
              confidence, proposed_by, origin_mode, status, created_at
            ) VALUES (
              %s, %s, %s, 'stale insight', 'test',
              0.5, %s, 'Self', 'pending', %s
            )
            """,
            (pid, profile_id, REFLECTION_FACET, auth.actor_id, old),
        )
        n = expire_stale_reflection_proposals(conn, profile_id, stale_days=30)
        row = fetch_one(conn, "SELECT status FROM personality_proposals WHERE id = %s", (pid,))
        conn.commit()

    assert n == 1
    assert row["status"] == "expired"


def test_persona_reflect_worker_job(ensure_db, monkeypatch):
    apply_migrations()
    get_settings.cache_clear()
    auth = resolve_auth(f"Bearer {get_settings().bootstrap_token.get_secret_value()}")

    with connection() as conn:
        _ensure_active_profile(conn, auth.subject_id)
        _cleanup_chat_sessions(conn, auth.subject_id)
        _cleanup_reflection_proposals(conn, auth.subject_id)
        conn.commit()

    _seed_chat_turns(
        auth,
        mode="Explorer",
        lines=[
            "Explorer mode — suggest a reversible test for the demo puzzle.",
            "Try a small synthetic batch.",
            "Record the result before changing another variable.",
        ],
    )
    monkeypatch.setattr(
        "virtual_self.observability.notify.notify_user",
        lambda *a, **k: True,
    )

    with transaction() as conn:
        jid = enqueue_persona_reflect(auth, reason="test", force=True)
    assert jid is not None

    handle_job(
        {
            "id": jid,
            "job_type": "persona.reflect",
            "payload_ref": {
                "subject_id": str(auth.subject_id),
                "actor_id": str(auth.actor_id),
                "notify": False,
            },
        }
    )

    with transaction() as conn:
        profile_id = fetch_one(
            conn,
            "SELECT id FROM personality_profiles WHERE subject_id = %s",
            (auth.subject_id,),
        )["id"]
        row = fetch_one(
            conn,
            """
            SELECT origin_mode FROM personality_proposals
            WHERE profile_id = %s AND facet_type = %s AND origin_mode = 'Explorer'
            """,
            (profile_id, REFLECTION_FACET),
        )
    assert row is not None

    with connection() as conn:
        _cleanup_chat_sessions(conn, auth.subject_id)
        _cleanup_reflection_proposals(conn, auth.subject_id)
        conn.commit()


def test_reflect_schedule_due_on_turn_threshold(ensure_db, monkeypatch):
    apply_migrations()
    get_settings.cache_clear()
    monkeypatch.setenv("VSM_PERSONA_REFLECT_TURN_THRESHOLD", "2")
    get_settings.cache_clear()
    auth = resolve_auth(f"Bearer {get_settings().bootstrap_token.get_secret_value()}")

    with transaction() as conn:
        due, reason = reflect_schedule_due(conn, auth.subject_id)
    assert due is True
    assert reason in ("interval", "turn_threshold")
