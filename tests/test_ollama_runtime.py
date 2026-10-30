from __future__ import annotations

from virtual_self.providers.ollama_runtime import (
    COLD_CHAT_MODELS,
    chat_model_status,
    parse_cold_index_models,
)


def test_cold_chat_models_defined():
    assert "qwen3.6:35b-a3b" in COLD_CHAT_MODELS
    assert "qwen3.6:27b" in COLD_CHAT_MODELS


def test_parse_cold_index_includes_qwen():
    cold = parse_cold_index_models()
    assert "qwen3.6:35b-a3b" in cold


def test_chat_model_status_not_loaded():
    st = chat_model_status("qwen3.6:35b-a3b")
    if not st.loaded:
        assert st.in_cold_index
        assert st.thaw_command == "ollama-cold thaw qwen3.6:35b-a3b"
