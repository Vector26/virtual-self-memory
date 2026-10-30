from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import virtual_self.providers.agent_chat as agent_chat
from virtual_self.services.chat_memory_extract import extract_chat_memories


def _classifier_settings(**overrides: object) -> SimpleNamespace:
    base = {
        "chat_memory_llm_backend": "ollama",
        "chat_memory_model": "qwen2.5:7b",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_extract_chat_memories_uses_classifier_settings(monkeypatch):
    turns = [{"role": "user", "content": "I live in Austin."}]
    llm_out = {
        "candidates": [
            {
                "content": "User lives in Austin",
                "kind": "fact",
                "source_speaker": "user",
                "should_persist": True,
                "session_only": False,
                "confidence": 0.9,
            }
        ]
    }

    monkeypatch.setattr(agent_chat, "get_settings", lambda: _classifier_settings())

    with patch(
        "virtual_self.providers.agent_chat.chat_json_for_task",
        return_value=llm_out,
    ) as mock_json:
        validated, raw = extract_chat_memories(
            turns,
            user_message="I live in Austin.",
            assistant_message="Got it.",
        )

    mock_json.assert_called_once()
    _, kwargs = mock_json.call_args
    assert kwargs["backend"] == "ollama"
    assert kwargs["model"] == "qwen2.5:7b"
    assert kwargs["openai_model"] == "qwen2.5:7b"
    assert raw == llm_out
    assert len(validated) == 1
    assert validated[0]["content"] == "User lives in Austin"


def test_classifier_chat_json_delegates_to_task_helper(monkeypatch):
    from virtual_self.providers.agent_chat import classifier_chat_json

    monkeypatch.setattr(
        agent_chat,
        "get_settings",
        lambda: _classifier_settings(chat_memory_model="custom-classifier"),
    )

    with patch(
        "virtual_self.providers.agent_chat.chat_json_for_task",
        return_value={"ok": True},
    ) as mock_task:
        out = classifier_chat_json("sys", "user", temperature=0.1)

    assert out == {"ok": True}
    mock_task.assert_called_once_with(
        "sys",
        "user",
        temperature=0.1,
        backend="ollama",
        model="custom-classifier",
        openai_model="custom-classifier",
    )
