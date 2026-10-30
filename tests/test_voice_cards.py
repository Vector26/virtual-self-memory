from __future__ import annotations

import json

import pytest
from uuid6 import uuid7

from virtual_self.auth.credentials import resolve_auth
from virtual_self.config import get_settings
from virtual_self.domain.errors import ConflictError
from virtual_self.persistence.db import connection, fetch_one, transaction
from virtual_self.persistence.migrate import apply_migrations
from virtual_self.services.proposal_review import approve_personality_proposal
from virtual_self.services.voice_cards import (
    build_voice_card_v2,
    propose_voice_card_v2,
    validate_voice_card,
    VOICE_CARD_VERSION,
)


def _seed_train_corpus(auth, *, register: str, count: int = 35) -> list[str]:
    ids: list[str] = []
    filler = (
        "I rebuilt the homelab stack with docker compose. "
        "Not sure the old config still applies but the goal is automation. "
        "basically keep it direct."
    )
    with connection() as conn:
        profile = fetch_one(
            conn,
            "SELECT id FROM personality_profiles WHERE subject_id = %s AND status = 'active' LIMIT 1",
            (auth.subject_id,),
        )
        if not profile:
            conn.execute(
                """
                INSERT INTO personality_profiles (id, subject_id, name, status, activated_at)
                VALUES (%s, %s, 'test', 'active', NOW())
                """,
                (uuid7(), auth.subject_id),
            )
        facet = fetch_one(
            conn,
            """
            SELECT f.id, f.current_version_id
            FROM personality_facets f
            JOIN personality_profiles p ON p.id = f.profile_id
            WHERE p.subject_id = %s AND f.facet_type = 'voice'
            LIMIT 1
            """,
            (auth.subject_id,),
        )
        if not facet:
            profile_id = fetch_one(
                conn,
                "SELECT id FROM personality_profiles WHERE subject_id = %s AND status = 'active' LIMIT 1",
                (auth.subject_id,),
            )["id"]
            facet_id = uuid7()
            ver_id = uuid7()
            conn.execute(
                "INSERT INTO personality_facets (id, profile_id, facet_type, mutability) VALUES (%s, %s, 'voice', 'approval_required')",
                (facet_id, profile_id),
            )
            conn.execute(
                """
                INSERT INTO personality_facet_versions (
                  id, facet_id, version, content, priority, confidence, status, approved_at
                ) VALUES (%s, %s, 1, 'bootstrap voice', 80, 0.5, 'active', NOW())
                """,
                (ver_id, facet_id),
            )
            conn.execute(
                "UPDATE personality_facets SET current_version_id = %s WHERE id = %s",
                (ver_id, facet_id),
            )

        for i in range(count):
            aid = uuid7()
            body = f"{filler} entry {i}."
            conn.execute(
                """
                INSERT INTO authored_texts (
                  id, subject_id, source_kind, source_ref, author, author_confidence,
                  register, content, word_count, text_hash, sensitivity, split
                ) VALUES (
                  %s, %s, 'notion', '{}'::jsonb, 'self', 1.0,
                  %s, %s, %s, %s, 1, 'train'
                )
                ON CONFLICT (subject_id, text_hash) DO NOTHING
                """,
                (aid, auth.subject_id, register, body, 30, uuid7().hex + str(i)),
            )
            ids.append(str(aid))

        conn.execute(
            """
            INSERT INTO style_profiles (
              id, subject_id, profile_id, register, version, features, n_texts, n_words
            )
            SELECT %s, %s, p.id, %s, 1,
              '{"sufficient": true, "n_words": 8000, "n_texts": 35,
                "length": {"words_per_message": {"mean": 30, "p50": 30}},
                "question_rate": 0.02,
                "lexicon": {"hedges_per_100_words": 1.2, "pet_phrases": [{"phrase": "homelab stack", "count": 5}]},
                "punctuation_per_100_words": {}}'::jsonb,
              35, 8000
            FROM personality_profiles p
            WHERE p.subject_id = %s AND p.status = 'active'
            LIMIT 1
            ON CONFLICT (profile_id, register, version) DO UPDATE SET
              features = EXCLUDED.features, n_words = EXCLUDED.n_words
            """,
            (uuid7(), auth.subject_id, register, auth.subject_id),
        )
        conn.commit()
    return ids


def _cleanup_voice_proposals(conn, subject_id):
    conn.execute(
        """
        DELETE FROM personality_proposal_evidence
        WHERE proposal_id IN (
          SELECT pp.id FROM personality_proposals pp
          JOIN personality_profiles prof ON prof.id = pp.profile_id
          WHERE prof.subject_id = %s AND pp.facet_type = 'voice'
        )
        """,
        (subject_id,),
    )
    conn.execute(
        """
        DELETE FROM personality_proposals pp
        USING personality_profiles prof
        WHERE pp.profile_id = prof.id AND prof.subject_id = %s AND pp.facet_type = 'voice'
        """,
        (subject_id,),
    )


def test_build_voice_card_v2_all_rules_have_evidence(ensure_db):
    apply_migrations()
    get_settings.cache_clear()
    token = get_settings().bootstrap_token
    auth = resolve_auth(f"Bearer {token.get_secret_value()}")
    _seed_train_corpus(auth, register="journal")
    _seed_train_corpus(auth, register="technical")

    card = build_voice_card_v2(auth)
    assert card.card_version == VOICE_CARD_VERSION
    assert len(card.rules) >= 10
    assert len(card.pet_phrases) >= 3
    validate_voice_card(card)
    for rule in card.rules:
        assert rule.evidence_authored_text_ids


def test_propose_voice_card_pending_not_active(ensure_db, monkeypatch):
    apply_migrations()
    get_settings.cache_clear()
    token = get_settings().bootstrap_token
    auth = resolve_auth(f"Bearer {token.get_secret_value()}")
    _seed_train_corpus(auth, register="journal")

    with transaction() as conn:
        _cleanup_voice_proposals(conn, auth.subject_id)

    monkeypatch.setattr(
        "virtual_self.services.voice_cards.notify_personality_proposal",
        lambda **kwargs: True,
    )

    with transaction() as conn:
        before_facet = fetch_one(
            conn,
            """
            SELECT v.content, v.version
            FROM personality_facets f
            JOIN personality_facet_versions v ON v.id = f.current_version_id
            JOIN personality_profiles p ON p.id = f.profile_id
            WHERE p.subject_id = %s AND f.facet_type = 'voice'
            """,
            (auth.subject_id,),
        )

    out = propose_voice_card_v2(auth, dry_run=False, notify=True)
    assert out["status"] == "pending"
    assert out["facet_type"] == "voice"

    with transaction() as conn:
        prop = fetch_one(
            conn,
            """
            SELECT pp.status, pp.facet_type, pp.origin_mode
            FROM personality_proposals pp
            JOIN personality_profiles prof ON prof.id = pp.profile_id
            WHERE pp.id = %s
            """,
            (out["proposal_id"],),
        )
        facet = fetch_one(
            conn,
            """
            SELECT v.content, v.version
            FROM personality_facets f
            JOIN personality_facet_versions v ON v.id = f.current_version_id
            JOIN personality_profiles p ON p.id = f.profile_id
            WHERE p.subject_id = %s AND f.facet_type = 'voice'
            """,
            (auth.subject_id,),
        )
        ev_count = fetch_one(
            conn,
            "SELECT COUNT(*) AS n FROM personality_proposal_evidence WHERE proposal_id = %s",
            (out["proposal_id"],),
        )

    assert prop["status"] == "pending"
    assert prop["origin_mode"] == "Self"
    assert facet["content"] == before_facet["content"]
    assert facet["version"] == before_facet["version"]
    assert int(ev_count["n"]) > 0

    with pytest.raises(ConflictError):
        propose_voice_card_v2(auth, dry_run=False, notify=False)


def test_approve_voice_proposal_bumps_facet_version(ensure_db, monkeypatch):
    apply_migrations()
    get_settings.cache_clear()
    token = get_settings().bootstrap_token
    auth = resolve_auth(f"Bearer {token.get_secret_value()}")
    _seed_train_corpus(auth, register="journal")

    with transaction() as conn:
        _cleanup_voice_proposals(conn, auth.subject_id)

    monkeypatch.setattr(
        "virtual_self.services.voice_cards.notify_personality_proposal",
        lambda **kwargs: True,
    )
    out = propose_voice_card_v2(auth, dry_run=False, notify=False)
    pid = out["proposal_id"]

    result = approve_personality_proposal(auth, __import__("uuid").UUID(pid))
    assert result["status"] == "approved"
    assert result["voice_apply"]["facet_version"] >= 2

    with transaction() as conn:
        facet = fetch_one(
            conn,
            """
            SELECT v.content, v.version, v.status
            FROM personality_facets f
            JOIN personality_facet_versions v ON v.id = f.current_version_id
            JOIN personality_profiles p ON p.id = f.profile_id
            WHERE p.subject_id = %s AND f.facet_type = 'voice'
            """,
            (auth.subject_id,),
        )
        payload = fetch_one(
            conn,
            "SELECT proposed_content FROM personality_proposals WHERE id = %s",
            (pid,),
        )

    assert facet["status"] == "active"
    assert facet["version"] >= 2
    assert "Voice card" in facet["content"] or "voice" in facet["content"].lower()
    data = json.loads(payload["proposed_content"])
    assert data["card_version"] == VOICE_CARD_VERSION
