"""compile_context + memory search integration (all scopes, including archive)."""

from __future__ import annotations

from uuid import uuid4

from virtual_self.auth.credentials import resolve_auth
from virtual_self.config import get_settings
from virtual_self.services.memories import create_memory, search_memories
from virtual_self.services.personality import bootstrap_personality, compile_context


def test_compile_context_merges_query_hits_with_pinned_chat(ensure_db, client, auth_headers):
    get_settings.cache_clear()
    auth = resolve_auth(auth_headers["Authorization"])
    bootstrap_personality(auth, name="Analyst")

    archive_needle = f"compile context archive scope {uuid4()}"
    create_memory(
        auth,
        kind="goal",
        content=archive_needle,
        scope="archive",
        trust="imported",
    )
    chat_needle = f"compile context chat pin {uuid4()}"
    create_memory(
        auth,
        kind="fact",
        content=chat_needle,
        scope="chat",
        trust="user_asserted",
    )

    bundle = compile_context(auth, query=archive_needle, memory_limit=15)
    texts = [m.get("content") or "" for m in bundle["relevant_memories"]]
    assert any(archive_needle in t for t in texts)
    assert any(chat_needle in t for t in texts), "query merge should include pinned chat memory"

    chat_hits = search_memories(auth, chat_needle, limit=5)
    assert any(chat_needle in (h.get("content") or "") for h in chat_hits)

    for m in bundle["relevant_memories"]:
        assert m.get("id") == m.get("memory_id")

    low = compile_context(auth, query=archive_needle, memory_limit=1)
    assert any(archive_needle in (m.get("content") or "") for m in low["relevant_memories"])

    api = client.post(
        "/v1/context:compile",
        json={"query": archive_needle, "memory_limit": 15},
        headers=auth_headers,
    )
    assert api.status_code == 200
    api_body = api.json()
    api_texts = [m.get("content") or "" for m in api_body["relevant_memories"]]
    assert any(archive_needle in t for t in api_texts)
    assert any(chat_needle in t for t in api_texts)
    assert "expires_at" in api_body
    assert "_approx_chars" not in api_body
    assert "approx_chars" in api_body.get("provenance", {})


def test_compile_context_token_budget_trims_memories(ensure_db, auth_headers):
    auth = resolve_auth(auth_headers["Authorization"])
    bootstrap_personality(auth, name="Analyst")
    for i in range(6):
        create_memory(
            auth,
            kind="fact",
            content=f"budget filler memory number {i} " + ("x" * 200),
            scope="private",
            trust="user_asserted",
        )
    bundle = compile_context(auth, query="", memory_limit=6, token_budget=300)
    assert bundle["provenance"].get("memories_trimmed_for_budget") is True
    assert len(bundle["relevant_memories"]) < 6
    assert bundle["provenance"]["token_budget"] == 300


def test_compile_context_large_personality_keeps_memories(ensure_db, auth_headers, monkeypatch):
    import virtual_self.services.personality as personality_mod

    auth = resolve_auth(auth_headers["Authorization"])
    bootstrap_personality(auth, name="Analyst")
    real = personality_mod.get_active_personality(auth)
    huge = {**real, "facets": [{"type": "traits", "content": "t" * 40000}]}
    monkeypatch.setattr(personality_mod, "get_active_personality", lambda _auth: huge)
    marker = f"zebra-lighthouse-{uuid4().hex[:8]}"
    create_memory(
        auth,
        kind="fact",
        content=f"{marker} lives with two roommates",
        scope="private",
        trust="user_asserted",
    )
    bundle = compile_context(auth, query=marker, memory_limit=6)
    assert any(marker in m["content"] for m in bundle["relevant_memories"])
