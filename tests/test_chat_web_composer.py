"""W-8: composer web integration (explicit trigger + optional auto gather)."""

from __future__ import annotations

from pathlib import Path

import pytest

from virtual_self.services.chat_reply import (
    _compose_turn_user,
    new_web_knowledge_turn,
)
from virtual_self.services.chat_web import (
    fence_web_notes,
    gather_web_for_turn,
    maybe_gather_web,
    plan_explicit_search_queries,
    user_explicit_web_request,
    web_gather_allowed,
)
from virtual_self.web.client import KnowledgeGatewayClient, normalize_url

SAMPLE_SEARCH = """
<sec:fence rating="untrusted">
Title: Python 3.14 release
URL: https://example.com/python314
Snippet: release notes
Engines: google
</sec:fence>
"""

SAMPLE_READ = """
<sec:fence rating="untrusted">
Python 3.14 was released in October 2026 with faster asyncio.
</sec:fence>
"""


def test_explicit_web_trigger_phrases():
    assert user_explicit_web_request("please look it up: latest python release")
    assert user_explicit_web_request("search for searxng docker image")
    assert not user_explicit_web_request("how are you today?")


def test_fence_web_notes_wraps_untrusted():
    body = "[1] Example\nURL: https://example.com\nHello"
    out = fence_web_notes(body)
    assert '<sec:fence rating="untrusted">' in out
    assert "untrusted" in out.lower()
    assert "[1]" in out


def test_web_gather_blocked_when_private_memory_without_explicit():
    bundle = {"relevant_memories": [{"sensitivity": 2, "content": "private journal"}]}
    assert not web_gather_allowed(bundle, user_explicit=False)
    assert web_gather_allowed(bundle, user_explicit=True)


def test_plan_explicit_queries_fallback_without_llm(monkeypatch):
    monkeypatch.setattr(
        "virtual_self.services.chat_web.chat_json_for_task",
        lambda *a, **k: None,
    )
    qs = plan_explicit_search_queries("look it up: Python 3.14 release date")
    assert qs == ["Python 3.14 release date"]


def test_compose_turn_user_includes_web_block():
    bundle = {"relevant_memories": []}
    web = fence_web_notes("[1] note")
    user_msg = _compose_turn_user(bundle, "hello", web_block=web)
    assert "Retrieved memories:" in user_msg
    assert "sec:fence" in user_msg
    assert user_msg.endswith("User: hello")


@pytest.mark.asyncio
async def test_gather_web_search_read_mocked(monkeypatch, tmp_path: Path):
    turn, client = new_web_knowledge_turn("composer-1", "look it up: python")
    client.audit_path = tmp_path / "audit.jsonl"
    client.token = "x" * 32

    async def fake_tool(name: str, arguments: dict):
        if name == "searxng_web_search":
            return False, SAMPLE_SEARCH
        if name == "searxng_read_url":
            assert arguments["url"] == normalize_url("https://example.com/python314")
            return False, SAMPLE_READ
        raise AssertionError(name)

    monkeypatch.setattr(client, "_mcp_tool", fake_tool)
    monkeypatch.setattr(
        "virtual_self.services.chat_web.plan_explicit_search_queries",
        lambda msg: ["python 3.14 release"],
    )
    monkeypatch.setattr(
        "virtual_self.services.chat_web.plan_auto_web_gather",
        lambda msg: (False, []),
    )

    result = await maybe_gather_web(turn, client, "look it up: python", {"relevant_memories": []})
    assert result.status == "gathered"
    assert result.trigger == "explicit"
    assert "sec:fence" in result.composer_block
    assert "asyncio" in result.composer_block
    assert len(result.sources) == 1
    assert result.sources[0].url == normalize_url("https://example.com/python314")


@pytest.mark.asyncio
async def test_gather_web_gateway_down_fail_soft(tmp_path: Path):
    turn, client = new_web_knowledge_turn("composer-2", "search for foo")
    client.audit_path = tmp_path / "audit.jsonl"
    client.token = None
    result = await gather_web_for_turn(turn, client, "search for foo", ["foo"])
    assert result.status == "unavailable"
    assert "cannot reach the web" in result.fail_soft_system_note.lower()


@pytest.mark.asyncio
async def test_auto_gather_when_model_plans_web(monkeypatch, tmp_path: Path):
    turn, client = new_web_knowledge_turn("composer-3", "What's the current stable Rust version?")
    client.audit_path = tmp_path / "audit.jsonl"
    client.token = "x" * 32

    async def fake_tool(name: str, arguments: dict):
        if name == "searxng_web_search":
            return False, SAMPLE_SEARCH
        if name == "searxng_read_url":
            return False, SAMPLE_READ
        raise AssertionError(name)

    monkeypatch.setattr(client, "_mcp_tool", fake_tool)
    monkeypatch.setattr(
        "virtual_self.services.chat_web.plan_auto_web_gather",
        lambda msg: (True, ["rust stable version"]),
    )

    result = await maybe_gather_web(
        turn,
        client,
        "What's the current stable Rust version?",
        {"relevant_memories": []},
    )
    assert result.trigger == "auto"
    assert result.status == "gathered"


@pytest.mark.asyncio
async def test_auto_gather_skipped_on_private_bundle(monkeypatch):
    turn, client = new_web_knowledge_turn("composer-4", "What's the current stable Rust version?")
    client.token = "x" * 32
    monkeypatch.setattr(
        "virtual_self.services.chat_web.plan_auto_web_gather",
        lambda msg: (True, ["rust stable"]),
    )
    bundle = {"relevant_memories": [{"sensitivity": 2, "content": "journal"}]}
    result = await maybe_gather_web(
        turn,
        client,
        "What's the current stable Rust version?",
        bundle,
    )
    assert result.status == "skipped"
