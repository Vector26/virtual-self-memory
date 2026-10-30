from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Literal

from virtual_self.config import get_settings
from virtual_self.domain.errors import ValidationError
from virtual_self.observability.logging import get_logger

log = get_logger(__name__)

_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")


def _openai_request_headers() -> dict[str, str]:
    settings = get_settings()
    headers = {"Content-Type": "application/json"}
    if settings.openai_api_key is not None:
        headers["Authorization"] = f"Bearer {settings.openai_api_key.get_secret_value()}"
    if "openrouter.ai" in settings.openai_base_url:
        headers["HTTP-Referer"] = "https://github.com/virtual-self-memory"
        headers["X-Title"] = "virtual-self-memory"
    return headers


def _http_json_post(url: str, payload: dict, timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=_openai_request_headers(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise ValidationError(f"LLM HTTP {exc.code}: {detail}") from exc


def _openai_available(base: str) -> bool:
    try:
        req = urllib.request.Request(
            f"{base.rstrip('/')}/models",
            headers=_openai_request_headers(),
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status == 200
    except OSError:
        return False


def _resolve_backend(
    backend: Literal["auto", "ollama", "openai"],
    *,
    openai_base_url: str,
) -> Literal["ollama", "openai"]:
    if backend == "auto":
        return "openai" if _openai_available(openai_base_url) else "ollama"
    return backend


def _complete_messages(
    system: str,
    messages: list[dict[str, str]],
    *,
    temperature: float,
    json_mode: bool,
    backend: Literal["auto", "ollama", "openai"] | None = None,
    model: str | None = None,
    openai_model: str | None = None,
    timeout_seconds: float | None = None,
) -> str:
    settings = get_settings()
    backend = _resolve_backend(
        backend if backend is not None else settings.llm_backend,
        openai_base_url=settings.openai_base_url,
    )
    chat_model = model if model is not None else settings.chat_model
    oai_model = openai_model if openai_model is not None else settings.openai_model
    timeout = timeout_seconds if timeout_seconds is not None else settings.chat_timeout_seconds

    if backend == "openai":
        msgs = [{"role": "system", "content": system}, *messages]
        payload: dict[str, Any] = {
            "model": oai_model,
            "messages": msgs,
            "temperature": temperature,
            "stream": False,
            # Qwen3.6 via llama-server defaults to long reasoning traces — disable for batch enrich.
            "max_tokens": 512 if json_mode else 768,
        }
        if "qwen" in oai_model.lower() or "qwen" in chat_model.lower():
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        url = f"{settings.openai_base_url.rstrip('/')}/chat/completions"
        body = _http_json_post(url, payload, timeout)
        choice = (body.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        text = str(msg.get("content") or msg.get("reasoning_content") or "").strip()
        return text

    payload = {
        "model": chat_model,
        "stream": False,
        "options": {"temperature": temperature},
        "messages": [{"role": "system", "content": system}, *messages],
    }
    if json_mode:
        payload["format"] = "json"
    url = f"{settings.ollama_url.rstrip('/')}/api/chat"
    body = _http_json_post(url, payload, timeout)
    if body.get("error"):
        raise ValidationError(f"Ollama chat error: {body['error']}")
    return ((body.get("message") or {}).get("content") or "").strip()


def preflight_llm() -> str:
    """One cheap completion; fail fast before enriching hundreds of pages."""
    settings = get_settings()
    try:
        out = _complete_messages(
            "Reply with exactly: ok",
            [{"role": "user", "content": "ping"}],
            temperature=0.0,
            json_mode=False,
        )
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError(
            "No working LLM for enrichment. Start Local AI (llama-server :8081) or upgrade Ollama "
            "and re-thaw cold models (see your local model manager)."
        ) from exc
    if not out:
        raise ValidationError("LLM preflight returned empty response.")
    backend = settings.llm_backend
    if backend == "auto":
        backend = "openai" if _openai_available(settings.openai_base_url) else "ollama"
    return backend


def chat_text(
    system: str,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.4,
) -> str | None:
    try:
        return _complete_messages(system, messages, temperature=temperature, json_mode=False) or None
    except ValidationError as exc:
        log.warning("agent_chat_text_failed", error=str(exc))
        return None


def chat_json(system: str, user: str, *, temperature: float = 0.2) -> dict[str, Any] | None:
    return chat_json_for_task(system, user, temperature=temperature)


def chat_json_for_task(
    system: str,
    user: str,
    *,
    temperature: float = 0.2,
    backend: Literal["auto", "ollama", "openai"] | None = None,
    model: str | None = None,
    openai_model: str | None = None,
) -> dict[str, Any] | None:
    try:
        content = _complete_messages(
            system,
            [{"role": "user", "content": user}],
            temperature=temperature,
            json_mode=True,
            backend=backend,
            model=model,
            openai_model=openai_model,
        )
    except ValidationError as exc:
        log.warning("agent_chat_failed", error=str(exc))
        return None
    if not content:
        return None
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = _JSON_BLOCK.search(content)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None


def classifier_chat_json(system: str, user: str, *, temperature: float = 0.2) -> dict[str, Any] | None:
    """JSON completion for chat.memory.extract — uses VSM_CHAT_MEMORY_* settings."""
    settings = get_settings()
    return chat_json_for_task(
        system,
        user,
        temperature=temperature,
        backend=settings.chat_memory_llm_backend,
        model=settings.chat_memory_model,
        openai_model=settings.chat_memory_model,
    )
