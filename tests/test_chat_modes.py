from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import UUID

import pytest
from uuid6 import uuid7

from virtual_self.auth.credentials import resolve_auth
from virtual_self.config import get_settings
from virtual_self.domain.errors import NotFoundError, ValidationError
from virtual_self.persistence.db import connection, execute, fetch_one, transaction
from virtual_self.persistence.migrate import apply_migrations
from virtual_self.services.chat_modes import (
    SessionModeState,
    composer_mode_section,
    load_approved_mode_block,
    parse_mode_command,
    resolve_mode_name,
)
from virtual_self.services.chat_reply import _system_prompt
from virtual_self.services.mode_cards import propose_mode_cards
from virtual_self.services.proposal_review import approve_personality_proposal


def _load_mode_card_test_helpers():
    spec = importlib.util.spec_from_file_location(
        "test_mode_cards_helpers",
        Path(__file__).with_name("test_mode_cards.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _delete_analyst_mode(conn, subject_id: UUID) -> None:
    conn.execute(
        "UPDATE persona_modes SET current_version_id = NULL WHERE subject_id = %s AND name = 'Analyst'",
        (subject_id,),
    )
    conn.execute(
        """
        DELETE FROM persona_mode_evidence
        WHERE mode_version_id IN (
          SELECT v.id FROM persona_mode_versions v
          JOIN persona_modes m ON m.id = v.mode_id
          WHERE m.subject_id = %s AND m.name = 'Analyst'
        )
        """,
        (subject_id,),
    )
    conn.execute(
        """
        DELETE FROM persona_mode_versions
        WHERE mode_id IN (SELECT id FROM persona_modes WHERE subject_id = %s AND name = 'Analyst')
        """,
        (subject_id,),
    )
    conn.execute(
        "DELETE FROM persona_modes WHERE subject_id = %s AND name = 'Analyst'",
        (subject_id,),
    )


def test_parse_mode_commands_explicit_only():
    cmd = parse_mode_command("/mode analyst — why infra again?")
    assert cmd is not None
    assert cmd.kind == "set"
    assert cmd.mode == "Analyst"
    assert "infra" in cmd.user_query

    off = parse_mode_command("/mode off")
    assert off is not None and off.kind == "off"

    council = parse_mode_command("/council — should I take the job?")
    assert council is not None and council.kind == "council"

    assert parse_mode_command("explain Analyst to me") is None
    assert parse_mode_command("answer as guide") is None

    with pytest.raises(ValidationError):
        parse_mode_command("/mode unknown")


def test_session_mode_state_transitions():
    state = SessionModeState()
    cmd = parse_mode_command("/mode guide")
    assert cmd is not None
    state = state.apply(cmd)
    assert state.active_mode == "Guide"
    state = state.apply(parse_mode_command("/mode off"))  # type: ignore[arg-type]
    assert state.active_mode is None


def test_resolve_mode_aliases():
    assert resolve_mode_name("Explorer") == "Explorer"
    assert resolve_mode_name("ANALYST") == "Analyst"


def test_system_prompt_includes_mode_section():
    bundle = {"approved_personality": {"name": "Analyst", "facets": []}, "behavioral_constraints": []}
    block = "## Active mode: Analyst\n\nBlunt."
    prompt = _system_prompt(bundle, mode_section=block)
    assert "# Mode (manual" in prompt
    assert "Active mode: Analyst" in prompt
    assert "I am Analyst" in prompt
    assert "Be direct and warm" not in prompt


def _seed_minimal_mode(auth, conn) -> None:
    """One approved Analyst mode version for composer tests."""
    mode_id = uuid7()
    ver_id = uuid7()
    execute(
        conn,
        """
        INSERT INTO persona_modes (
          id, subject_id, name, role_summary, triggers, voice_delta, values_delta, status
        ) VALUES (
          %s, %s, 'Analyst', 'Blunt engine.', '[]'::jsonb, '{"register":"blunt"}'::jsonb,
          '{"emphasis":"execution"}'::jsonb, 'active'
        )
        """,
        (mode_id, auth.subject_id),
    )
    execute(
        conn,
        """
        INSERT INTO persona_mode_versions (
          id, mode_id, version, role_summary, triggers, voice_delta, values_delta, status
        ) VALUES (
          %s, %s, 1, 'Blunt engine.', '[]'::jsonb, '{"register":"blunt"}'::jsonb,
          '{"emphasis":"execution"}'::jsonb, 'active'
        )
        """,
        (ver_id, mode_id),
    )
    execute(
        conn,
        "UPDATE persona_modes SET current_version_id = %s WHERE id = %s",
        (ver_id, mode_id),
    )


def test_load_approved_mode_block_requires_active_version(ensure_db):
    apply_migrations()
    get_settings.cache_clear()
    auth = resolve_auth(f"Bearer {get_settings().bootstrap_token.get_secret_value()}")
    with connection() as conn:
        _delete_analyst_mode(conn, auth.subject_id)
        conn.commit()
    with pytest.raises(NotFoundError):
        load_approved_mode_block(auth, "Analyst")

    with connection() as conn:
        _seed_minimal_mode(auth, conn)
        conn.commit()

    block = load_approved_mode_block(auth, "Analyst")
    assert "Active mode: Analyst" in block
    section = composer_mode_section(auth, session_mode="Analyst", council=False)
    assert section == block


def test_proposal_pending_vs_persona_mode_lifecycle(ensure_db, monkeypatch):
    """persona_modes.status = lifecycle; proposals + version status = approval."""
    tmc = _load_mode_card_test_helpers()
    _cleanup_mode_proposals = tmc._cleanup_mode_proposals
    _ensure_active_profile = tmc._ensure_active_profile
    _seed_mode_evidence = tmc._seed_mode_evidence

    apply_migrations()
    get_settings.cache_clear()
    auth = resolve_auth(f"Bearer {get_settings().bootstrap_token.get_secret_value()}")

    with connection() as conn:
        _ensure_active_profile(conn, auth.subject_id)
        _cleanup_mode_proposals(conn, auth.subject_id)
        _seed_mode_evidence(conn, auth)
        conn.commit()

    monkeypatch.setattr(
        "virtual_self.services.mode_cards.notify_personality_proposal",
        lambda **kwargs: True,
    )
    out = propose_mode_cards(auth, dry_run=False, notify=False)

    with transaction() as conn:
        row = fetch_one(
            conn,
            """
            SELECT m.status AS lifecycle, v.status AS version_status, pp.status AS proposal_status
            FROM persona_modes m
            JOIN persona_mode_versions v ON v.id = m.current_version_id
            JOIN personality_proposals pp ON pp.id = %s
            WHERE m.subject_id = %s AND m.name = 'Analyst'
            """,
            (UUID(out["proposals"]["Analyst"]), auth.subject_id),
        )
    assert row["lifecycle"] == "active"
    assert row["proposal_status"] == "pending"
    assert row["version_status"] == "draft"

    approve_personality_proposal(auth, UUID(out["proposals"]["Analyst"]))
    with transaction() as conn:
        row2 = fetch_one(
            conn,
            """
            SELECT v.status AS version_status
            FROM persona_modes m
            JOIN persona_mode_versions v ON v.id = m.current_version_id
            WHERE m.subject_id = %s AND m.name = 'Analyst'
            """,
            (auth.subject_id,),
        )
    assert row2["version_status"] == "active"
    assert load_approved_mode_block(auth, "Analyst")
