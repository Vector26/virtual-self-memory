"""End-to-end: archive-scoped memories are searchable and appear in compile_context.

Usable archive memory (product definition exercised here):
- scope=archive, active version, artifact not deleted
- discoverable via search_memories / POST /v1/memories/search for a distinctive query
- included in compile_context(..., query=...) relevant_memories when the query matches
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from virtual_self.auth.credentials import resolve_auth
from virtual_self.config import get_settings
from virtual_self.persistence.db import fetch_one, transaction
from virtual_self.services.memories import create_memory, get_memory, search_memories
from virtual_self.services.personality import bootstrap_personality, compile_context


def _auth(ensure_db):
    get_settings.cache_clear()
    token = get_settings().bootstrap_token
    assert token is not None
    return resolve_auth(f"Bearer {token.get_secret_value()}")


def _db_memory_row(memory_id: UUID) -> dict:
    with transaction() as conn:
        row = fetch_one(
            conn,
            """
            SELECT m.scope, m.kind, mv.status, mv.content, a.deleted_at
            FROM memories m
            JOIN memory_versions mv ON mv.id = m.current_version_id
            JOIN artifacts a ON a.id = m.id
            WHERE m.id = %s
            """,
            (memory_id,),
        )
    assert row is not None
    return row


def assert_archive_memory_usable(
    auth,
    memory_id: UUID,
    *,
    needle: str,
    client=None,
    auth_headers=None,
) -> None:
    """Verify stored state and retrieval paths for one archive memory."""
    row = _db_memory_row(memory_id)
    assert row["scope"] == "archive"
    assert row["status"] == "active"
    assert row["deleted_at"] is None
    assert needle in row["content"]

    api = get_memory(auth, memory_id)
    assert api["scope"] == "archive"
    assert api["current_version"]["status"] == "active"

    hits = search_memories(auth, needle, limit=10)
    assert any(needle in (h.get("content") or "") for h in hits), "search_memories missed archive memory"

    if client is not None and auth_headers is not None:
        r = client.post(
            "/v1/memories/search",
            json={"query": needle, "limit": 10},
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert any(needle in (i.get("content") or "") for i in r.json()["items"])

    bootstrap_personality(auth, name="TestDefault")
    bundle = compile_context(auth, query=needle, memory_limit=12)
    rel = bundle.get("relevant_memories") or []
    assert any(needle in (m.get("content") or "") for m in rel), "compile_context missed archive memory"


@pytest.fixture
def auth(ensure_db):
    return _auth(ensure_db)


def test_create_archive_memory_usable(auth, client, auth_headers):
    needle = f"archive usable e2e marker {uuid4()}"
    api = create_memory(
        auth,
        kind="preference",
        content=needle,
        scope="archive",
        trust="imported",
        sensitivity=1,
    )
    mid = UUID(api["id"])
    assert api["scope"] == "archive"
    assert_archive_memory_usable(auth, mid, needle=needle, client=client, auth_headers=auth_headers)


def test_promote_archive_memory_usable(auth, monkeypatch):
    monkeypatch.setenv("VSM_ARCHIVE_MIN_CONFIDENCE", "0.5")
    get_settings.cache_clear()
    from virtual_self.archive import promote as promote_mod
    from virtual_self.archive.promote import promote_verified_candidates

    # Dedup check queries DB; isolate from prior test data with unrelated tokens.
    monkeypatch.setattr(promote_mod, "search_memories", lambda *_a, **_k: [])

    needle = f"xyzzy-promote-only-{uuid4()}"
    verified = [
        {
            "content": needle,
            "confidence": 0.92,
            "verdict": "supported",
            "kind": "preference",
            "evidence_event_ids": [],
        }
    ]
    stats = promote_verified_candidates(auth, verified, events_by_id={}, actions=None)
    assert stats["promoted"] == 1

    hits = search_memories(auth, needle[:30], limit=5)
    match = next(h for h in hits if needle in (h.get("content") or ""))
    assert_archive_memory_usable(auth, UUID(match["id"]), needle=needle)
