from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Sequence

import urllib.request

from virtual_self.config import get_settings
from virtual_self.domain.errors import ValidationError

# Matches your local model manager cold Ollama chat models.
COLD_CHAT_MODELS: tuple[str, ...] = ("qwen3.6:35b-a3b", "qwen3.6:27b")
_DEFAULT_FAST = "qwen3.6:35b-a3b"
_DEFAULT_SMART = "qwen3.6:27b"

_INDEX_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|", re.MULTILINE)


@dataclass(frozen=True)
class ChatModelStatus:
    requested: str
    loaded: bool
    in_cold_index: bool
    thaw_command: str | None


def list_loaded_ollama_models() -> set[str]:
    settings = get_settings()
    try:
        with urllib.request.urlopen(f"{settings.ollama_url.rstrip('/')}/api/tags", timeout=5) as resp:
            import json

            data = json.loads(resp.read().decode("utf-8"))
    except OSError:
        return set()
    return {m.get("name", "") for m in data.get("models", []) if m.get("name")}


def parse_cold_index_models() -> set[str]:
    try:
        out = subprocess.run(
            ["ollama-cold", "list"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    text = out.stdout or ""
    found: set[str] = set()
    for m in _INDEX_ROW.finditer(text):
        name = m.group(1).strip()
        if name and not name.startswith("Model"):
            found.add(name)
    return found | set(COLD_CHAT_MODELS)


def chat_model_status(model: str) -> ChatModelStatus:
    loaded = model in list_loaded_ollama_models()
    cold = parse_cold_index_models()
    in_cold = model in cold
    thaw = f"ollama-cold thaw {model}" if in_cold and not loaded else None
    return ChatModelStatus(
        requested=model,
        loaded=loaded,
        in_cold_index=in_cold,
        thaw_command=thaw,
    )


def ensure_chat_model(model: str | None = None, *, thaw: bool = False) -> str:
    settings = get_settings()
    name = model or settings.chat_model
    st = chat_model_status(name)
    if st.loaded:
        return name
    if thaw and st.thaw_command:
        subprocess.run(
            ["ollama-cold", "thaw", name],
            check=True,
            timeout=600,
        )
        if name in list_loaded_ollama_models():
            return name
    hints: list[str] = []
    if st.thaw_command:
        hints.append(st.thaw_command)
    else:
        hints.append(f"ollama pull {name}")
    loaded = list_loaded_ollama_models()
    cold = sorted(parse_cold_index_models())
    msg = (
        f"Ollama chat model not loaded: {name}. "
        f"Loaded: {sorted(loaded) or '(none)'}. "
        f"Cold chat models: {cold or '(check ollama-cold list)'}. "
        f"Try: {'; '.join(hints)}"
    )
    raise ValidationError(msg)


def pick_default_chat_model(prefer: str = "fast") -> str:
    """Prefer cold MoE for batch enrichment, dense 27B for synthesis."""
    return _DEFAULT_FAST if prefer == "fast" else _DEFAULT_SMART
