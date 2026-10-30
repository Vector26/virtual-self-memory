from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from virtual_self.auth.credentials import AuthContext, resolve_auth
from virtual_self.config import get_settings
from virtual_self.services.chat_memory_promote import promote_candidate, run_chat_memory_extract
from virtual_self.services.chat_sessions import append_turn, open_cli_session
from virtual_self.services.memories import search_memories


@pytest.fixture
def auth(ensure_db):
    get_settings.cache_clear()
    token = get_settings().bootstrap_token
    assert token is not None
    return resolve_auth(f"Bearer {token.get_secret_value()}")


def test_promote_candidate_respects_confidence(auth, monkeypatch):
    monkeypatch.setenv("VSM_CHAT_MEMORY_MIN_CONFIDENCE", "0.99")
    get_settings.cache_clear()
    low = promote_candidate(
        auth,
        {
            "content": "unique test fact xyzzy-low-conf",
            "kind": "fact",
            "confidence": 0.5,
        },
        evidence_record_id=None,
    )
    assert low is None


def test_promote_candidate_creates_searchable_memory(auth, monkeypatch):
    monkeypatch.setenv("VSM_CHAT_MEMORY_MIN_CONFIDENCE", "0.85")
    monkeypatch.setenv("VSM_CHAT_MEMORY_NOTIFY_ON_SAVE", "false")
    get_settings.cache_clear()
    content = f"unique chat memory promote test {uuid4()}"
    api = promote_candidate(
        auth,
        {"content": content, "kind": "fact", "confidence": 0.95},
        evidence_record_id=None,
    )
    assert api is not None
    assert UUID(api["id"])
    hits = search_memories(auth, content[:40], limit=5)
    assert any(content in (h.get("content") or "") for h in hits)


def test_run_chat_memory_extract_skips_assistant_turn(auth, ensure_db, monkeypatch):
    monkeypatch.setenv("VSM_CHAT_MEMORY_ENABLED", "true")
    get_settings.cache_clear()
    session_id = open_cli_session(auth)
    user = append_turn(auth, session_id, role="user", content="hello")
    asst = append_turn(auth, session_id, role="assistant", content="hi there")
    out = run_chat_memory_extract(auth, session_id, asst["turn_id"])
    assert out == {"decision": "skipped", "reason": "not_user_turn"}


def test_run_chat_memory_extract_excludes_assistant_candidates(auth, ensure_db, monkeypatch):
    monkeypatch.setenv("VSM_CHAT_MEMORY_ENABLED", "true")
    get_settings.cache_clear()
    session_id = open_cli_session(auth)
    user = append_turn(auth, session_id, role="user", content="just chatting")
    append_turn(auth, session_id, role="assistant", content="User loves windshields")
    llm_out = {
        "candidates": [
            {
                "content": "User loves windshields",
                "kind": "fact",
                "source_speaker": "assistant",
                "should_persist": True,
                "session_only": False,
                "confidence": 0.99,
            }
        ]
    }
    monkeypatch.setattr(
        "virtual_self.services.chat_memory_promote.extract_chat_memories",
        lambda *a, **k: ([], llm_out),
    )
    out = run_chat_memory_extract(auth, session_id, user["turn_id"])
    assert out == {"decision": "excluded", "reason": "assistant_derived_candidates"}


def test_run_chat_memory_extract_rejects_foreign_subject(auth, ensure_db, monkeypatch):
    monkeypatch.setenv("VSM_CHAT_MEMORY_ENABLED", "true")
    get_settings.cache_clear()
    session_id = open_cli_session(auth)
    user = append_turn(auth, session_id, role="user", content="hello")
    append_turn(auth, session_id, role="assistant", content="hi there")
    foreign = AuthContext(
        actor_id=auth.actor_id,
        subject_id=uuid4(),
        scopes=auth.scopes,
        sensitivity_max=auth.sensitivity_max,
    )
    out = run_chat_memory_extract(foreign, session_id, user["turn_id"])
    assert out["decision"] == "failed"
    assert out["reason"] == "session_subject_mismatch"
