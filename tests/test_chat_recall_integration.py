"""Chat recall integration: conversational queries → compile_context with People/imported memories."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

from virtual_self.auth.credentials import resolve_auth
from virtual_self.config import get_settings
from virtual_self.persistence.db import fetch_one, transaction
from virtual_self.services.memories import create_memory, search_memories
from virtual_self.services.personality import bootstrap_personality, compile_context
from virtual_self.services.retrieval_queries import queries_for_retrieval


def _auth(ensure_db):
    get_settings.cache_clear()
    return resolve_auth(f"Bearer {os.environ['VSM_BOOTSTRAP_TOKEN']}")


def _memory_scope(memory_id: UUID) -> str:
    with transaction() as conn:
        row = fetch_one(
            conn,
            "SELECT scope FROM memories WHERE id = %s",
            (memory_id,),
        )
    assert row is not None
    return row["scope"]


def _index_of_marker(memories: list[dict], marker: str) -> int:
    for i, m in enumerate(memories):
        if marker in (m.get("content") or ""):
            return i
    return -1


def test_queries_for_retrieval_includes_ananya_from_conversational_question():
    out = queries_for_retrieval("who was Ananya?")
    assert out[0].casefold().startswith("who was ananya")
    assert any("Ananya" in v for v in out)


def test_who_was_ananya_surfaces_imported_memory_in_compile_context(ensure_db):
    auth = _auth(ensure_db)
    bootstrap_personality(auth, name="Default")
    token = uuid4().hex[:8]
    imported_marker = f"chat-recall-imported-{token}"
    body = (
        f"Ananya ({imported_marker}) — relationship from People import. "
        "User describes her as a former partner; not on a pedestal, a person they loved."
    )
    create_memory(
        auth,
        kind="relationship",
        content=body,
        scope="imported",
        trust="imported",
    )

    bundle = compile_context(
        auth,
        query=f"who was Ananya? {imported_marker}",
        memory_limit=12,
    )
    rel = bundle.get("relevant_memories") or []
    assert any(imported_marker in (m.get("content") or "") for m in rel), (
        "compile_context should include this imported People memory for conversational name query"
    )


def test_imported_scope_ranks_above_archive_for_ananya_recall(ensure_db):
    """When both scopes match, imported My Life memory should outrank archive noise."""
    auth = _auth(ensure_db)
    bootstrap_personality(auth, name="Default")
    token = uuid4().hex[:8]
    rank_key = f"rankprobe-{token}"
    archive_marker = f"chat-recall-archive-{token}"
    imported_marker = f"chat-recall-imported-rank-{token}"

    archive_api = create_memory(
        auth,
        kind="episode",
        content=(
            f"Ananya {rank_key} {archive_marker}: vague archive activity log snippet."
        ),
        scope="archive",
        trust="imported",
    )
    imported_api = create_memory(
        auth,
        kind="relationship",
        content=(
            f"Ananya {rank_key} {imported_marker}: curated People page — role and how user describes her."
        ),
        scope="imported",
        trust="imported",
    )
    archive_id = UUID(archive_api["id"])
    imported_id = UUID(imported_api["id"])
    assert _memory_scope(archive_id) == "archive"
    assert _memory_scope(imported_id) == "imported"

    hits = search_memories(auth, rank_key, limit=20)
    ours = [h for h in hits if h.get("id") in {str(archive_id), str(imported_id)}]
    assert len(ours) == 2, "both scopes should match the same recall probe"
    assert ours[0]["id"] == str(imported_id), (
        "imported scope should rank above archive for the same person"
    )

    bundle = compile_context(auth, query=f"who was Ananya? {rank_key}", memory_limit=12)
    rel = bundle.get("relevant_memories") or []
    imp_idx = _index_of_marker(rel, imported_marker)
    arch_idx = _index_of_marker(rel, archive_marker)
    assert imp_idx >= 0 and arch_idx >= 0
    assert imp_idx < arch_idx, "compile_context should prefer imported over archive ordering"
