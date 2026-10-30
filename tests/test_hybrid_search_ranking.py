"""Hybrid retrieval ranking: ILIKE boost, scope multipliers, person queries."""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import pytest

from virtual_self.auth.credentials import resolve_auth
from virtual_self.services.memories import create_memory, search_memories
from virtual_self.services.personality import bootstrap_personality
from virtual_self.services.retrieval import (
    high_signal_tokens,
    is_archiveish_query,
    is_person_query,
    scope_rank_multiplier,
)


def test_high_signal_tokens_skip_stopwords_and_short():
    assert high_signal_tokens("who is the cat") == ["cat"]
    assert "the" not in [t.lower() for t in high_signal_tokens("theory of everything")]
    assert "of" not in high_signal_tokens("state of mind")


def test_is_person_query_uses_retrieval_variants():
    assert is_person_query("Who was Ada Lovelace?")
    assert not is_person_query("what is the weather today")


def test_scope_multiplier_archive_demoted_when_not_archiveish():
    assert scope_rank_multiplier("private", person_query=True, archiveish=False) == 1.0
    assert scope_rank_multiplier("archive", person_query=False, archiveish=False) == 0.35
    assert scope_rank_multiplier("archive", person_query=True, archiveish=False) == 0.35


def test_scope_multiplier_archive_full_when_archiveish():
    assert scope_rank_multiplier("archive", person_query=True, archiveish=True) == 1.0


def test_is_archiveish_query():
    assert is_archiveish_query("something in my google takeout")
    assert not is_archiveish_query("Who was Jane Doe?")


@patch("virtual_self.services.retrieval.embed_one", return_value=None)
def test_person_query_ranks_private_above_archive(mock_embed, ensure_db, auth_headers):
    auth = resolve_auth(auth_headers["Authorization"])
    bootstrap_personality(auth, name="Default")
    uid = uuid4().hex[:12]
    person = f"RankPerson {uid}"
    priv_content = f"{person} is my close friend from college."
    arch_content = f"{person} appeared once in an old export log entry."

    create_memory(
        auth,
        kind="relationship",
        content=arch_content,
        scope="archive",
        trust="imported",
    )
    create_memory(
        auth,
        kind="relationship",
        content=priv_content,
        scope="private",
        trust="user_asserted",
    )

    hits = search_memories(auth, f"Who was {person}?", limit=5)
    assert hits, "expected lexical hits"
    assert priv_content in hits[0]["content"]


@patch("virtual_self.services.retrieval.embed_one", return_value=None)
def test_ilike_boost_surfaces_token_match(mock_embed, ensure_db, auth_headers):
    auth = resolve_auth(auth_headers["Authorization"])
    bootstrap_personality(auth, name="Default")
    token = f"Zephyrtok{uuid4().hex[:8]}"
    strong = f"Notes mentioning {token} in the body."
    weak = "Generic relationship notes without the rare token."

    create_memory(auth, kind="fact", content=weak, scope="private")
    create_memory(auth, kind="fact", content=strong, scope="private")

    hits = search_memories(auth, token, limit=3)
    assert hits[0]["content"] == strong
