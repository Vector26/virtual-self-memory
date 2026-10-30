"""Composer v2 prompt assembly (VSELF-20)."""

from __future__ import annotations

import pytest

from virtual_self.services.chat_composer import (
    CHAT_HISTORY_K,
    compose_turn_user,
    expectations_system_prompt,
    late_composer_reminder,
    prepare_chat_history,
    render_memory_notes,
    sanitize_user_history_content,
    to_first_person_memory,
)


def test_expectations_system_prompt_first_person_voice_and_anti_patterns():
    bundle = {
        "approved_personality": {
            "name": "Analyst",
            "facets": [
                {"type": "voice", "content": "- Lead with the answer.\n- Short sentences."},
                {"type": "values", "content": "Craft over hype."},
            ],
        },
        "behavioral_constraints": ["Never invent facts."],
    }
    prompt = expectations_system_prompt(
        bundle,
        anti_patterns=["great question", "i hope this helps"],
    )
    assert "I am Analyst" in prompt
    assert "not a generic assistant" in prompt
    assert "Be direct and warm" not in prompt
    assert "# Voice (approved card)" in prompt
    assert "Lead with the answer" in prompt
    assert "Never: great question" in prompt
    assert "## values" in prompt


def test_to_first_person_memory():
    assert to_first_person_memory("The user maintains a DevOps page.") == (
        "I maintain a DevOps page."
    )
    assert "I keep" in to_first_person_memory("The user keeps a DevOps learning page.")


def test_render_memory_notes_dated_first_person():
    bundle = {
        "relevant_memories": [
            {
                "content": "The user maintains a DevOps learning resource.",
                "valid_from": "2024-01-15T00:00:00+00:00",
                "time_precision": "year",
            }
        ]
    }
    block = render_memory_notes(bundle)
    assert block.startswith("2024 · ")
    assert "the user" not in block.lower()
    assert "I " in block


def test_sanitize_user_history_strips_retrieved_memories():
    bloated = (
        "Retrieved memories:\n- old fact\n\n"
        "Web notes:\nuntrusted\n\n"
        "User: what is up?"
    )
    assert sanitize_user_history_content(bloated) == "what is up?"


def test_prepare_chat_history_keeps_last_k_and_strips_user_bloat():
    history = []
    for i in range(12):
        history.append({"role": "user", "content": f"Retrieved memories:\nold\n\nUser: u{i}"})
        history.append({"role": "assistant", "content": f"a{i}"})
    cleaned = prepare_chat_history(history, k=CHAT_HISTORY_K)
    assert len(cleaned) == CHAT_HISTORY_K
    assert cleaned[0]["content"] == "u8"
    assert cleaned[-1]["content"] == "a11"


def test_late_reminder_present_self_and_no_headers_rule():
    bundle = {
        "approved_personality": {
            "facets": [{"type": "voice", "content": "- One.\n- Two.\n- Three."}]
        }
    }
    text = late_composer_reminder(bundle)
    assert "Present self" in text
    assert "No headers or lists" in text
    assert "One." in text


def test_compose_turn_user_includes_memories_block():
    bundle = {"relevant_memories": [{"content": "The user likes rust.", "time_precision": "year"}]}
    msg = compose_turn_user(bundle, "hello")
    assert "Retrieved memories:" in msg
    assert msg.endswith("User: hello")
    assert " · " in msg


@pytest.mark.asyncio
async def test_generate_chat_reply_late_reminder_after_turn_user(monkeypatch, ensure_db, auth_headers):
    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.services.chat_reply import generate_chat_reply_async

    auth = resolve_auth(auth_headers["Authorization"])
    captured: dict[str, object] = {}

    def fake_chat(system: str, messages: list[dict[str, str]], **kwargs):
        captured["system"] = system
        captured["messages"] = messages
        return "ok"

    monkeypatch.setattr("virtual_self.services.chat_reply.chat_text", fake_chat)
    monkeypatch.setattr(
        "virtual_self.services.chat_reply.compile_context",
        lambda *a, **k: {
            "approved_personality": {"name": "Analyst", "facets": []},
            "behavioral_constraints": [],
            "relevant_memories": [],
        },
    )
    async def _fake_web(*_a, **_k):
        return type("W", (), {"fail_soft_system_note": "", "composer_block": ""})()

    monkeypatch.setattr("virtual_self.services.chat_reply.maybe_gather_web", _fake_web)

    history = [
        {
            "role": "user",
            "content": "Retrieved memories:\nstale\n\nUser: earlier question",
        },
        {"role": "assistant", "content": "old reply"},
    ]
    await generate_chat_reply_async(auth, "latest?", history=history)

    messages = captured["messages"]
    assert isinstance(messages, list)
    assert messages[0]["content"] == "earlier question"
    assert "Retrieved memories:" in messages[-2]["content"]
    assert "Reminder:" in messages[-1]["content"]
    assert "No headers or lists" in messages[-1]["content"]
