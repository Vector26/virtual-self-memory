from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from uuid6 import uuid7

from virtual_self.auth.credentials import resolve_auth
from virtual_self.config import get_settings
from virtual_self.domain.errors import ConflictError
from virtual_self.persistence.db import connection, execute, fetch_one, transaction
from virtual_self.persistence.migrate import apply_migrations
from virtual_self.services.mode_cards import (
    CANONICAL_MODES,
    MODE_CARD_VERSION,
    build_all_mode_cards,
    propose_mode_cards,
    validate_mode_card,
)
from virtual_self.services.proposal_review import approve_personality_proposal


def _insert_dated_memory(conn, auth, *, content: str, when: datetime) -> UUID:
    mem_id = uuid7()
    ver_id = uuid7()
    execute(
        conn,
        """
        INSERT INTO artifacts (id, artifact_type, subject_id, sensitivity)
        VALUES (%s, 'memory', %s, 1)
        """,
        (mem_id, auth.subject_id),
    )
    execute(
        conn,
        """
        INSERT INTO memories (
          id, subject_id, kind, scope, time_precision, valid_from, authored_at
        ) VALUES (%s, %s, 'fact', 'chat', 'day', %s, %s)
        """,
        (mem_id, auth.subject_id, when, when),
    )
    execute(
        conn,
        """
        INSERT INTO artifacts (id, artifact_type, subject_id, sensitivity)
        VALUES (%s, 'memory_version', %s, 1)
        """,
        (ver_id, auth.subject_id),
    )
    execute(
        conn,
        """
        INSERT INTO memory_versions (id, memory_id, version, content, status)
        VALUES (%s, %s, 1, %s, 'active')
        """,
        (ver_id, mem_id, content),
    )
    execute(conn, "UPDATE memories SET current_version_id = %s WHERE id = %s", (ver_id, mem_id))
    return mem_id


def _insert_authored(conn, auth, *, content: str, mode_hint: str | None = None) -> UUID:
    aid = uuid7()
    execute(
        conn,
        """
        INSERT INTO authored_texts (
          id, subject_id, source_kind, source_ref, author, author_confidence,
          register, content, word_count, text_hash, sensitivity, split, mode_hint
        ) VALUES (
          %s, %s, 'manual', '{}'::jsonb, 'self', 1.0,
          'journal', %s, %s, %s, 1, 'train', %s
        )
        ON CONFLICT (subject_id, text_hash) DO NOTHING
        """,
        (aid, auth.subject_id, content, len(content.split()), uuid7().hex, mode_hint),
    )
    return aid


def _insert_draft_digest(conn, auth, *, memory_ids: list[UUID], modes_text: str) -> UUID:
    strand = fetch_one(conn, "SELECT id FROM life_strands WHERE slug = 'place'")
    chapter_id = uuid7()
    execute(
        conn,
        """
        INSERT INTO life_chapters (
          id, subject_id, strand_id, title, summary, status,
          valid_from, valid_to, time_precision,
          valid_from_precision, valid_to_precision, edge_fuzz_days
        ) VALUES (
          %s, %s, %s, %s, %s, 'proposed',
          %s, NULL, 'month', 'month', 'unknown', 0
        )
        """,
        (
            chapter_id,
            auth.subject_id,
            strand["id"],
            "Modes era",
            "chapter",
            datetime(2025, 6, 1, tzinfo=timezone.utc),
        ),
    )
    digest_id = uuid7()
    execute(
        conn,
        """
        INSERT INTO life_chapter_guided_digests (
          id, subject_id, chapter_id, status, prompt_version, journaling_bias_note, metadata
        ) VALUES (%s, %s, %s, 'draft', 'test', NULL, '{}'::jsonb)
        """,
        (digest_id, auth.subject_id, chapter_id),
    )
    item_id = uuid7()
    execute(
        conn,
        """
        INSERT INTO life_chapter_guided_digest_items (
          id, digest_id, step, layer, text, stance, sort_order
        ) VALUES (%s, %s, 'profile_sentence', 'modes', %s, 'actual', 0)
        """,
        (item_id, digest_id, modes_text),
    )
    for mid in memory_ids:
        execute(
            conn,
            """
            INSERT INTO life_chapter_guided_digest_evidence (
              id, item_id, memory_id, relationship
            ) VALUES (%s, %s, %s, 'supports')
            """,
            (uuid7(), item_id, mid),
        )
    return digest_id


def _cleanup_mode_proposals(conn, subject_id: UUID) -> None:
    conn.execute(
        """
        DELETE FROM personality_proposal_evidence
        WHERE proposal_id IN (
          SELECT pp.id FROM personality_proposals pp
          JOIN personality_profiles prof ON prof.id = pp.profile_id
          WHERE prof.subject_id = %s AND pp.facet_type LIKE 'mode_%%'
        )
        """,
        (subject_id,),
    )
    conn.execute(
        """
        DELETE FROM personality_proposals pp
        USING personality_profiles prof
        WHERE pp.profile_id = prof.id
          AND prof.subject_id = %s
          AND pp.facet_type LIKE 'mode_%%'
        """,
        (subject_id,),
    )
    conn.execute(
        """
        DELETE FROM persona_mode_evidence
        WHERE mode_version_id IN (
          SELECT v.id FROM persona_mode_versions v
          JOIN persona_modes m ON m.id = v.mode_id
          WHERE m.subject_id = %s
        )
        """,
        (subject_id,),
    )
    conn.execute(
        "UPDATE persona_modes SET current_version_id = NULL WHERE subject_id = %s",
        (subject_id,),
    )
    conn.execute(
        "DELETE FROM persona_mode_versions WHERE mode_id IN (SELECT id FROM persona_modes WHERE subject_id = %s)",
        (subject_id,),
    )
    conn.execute("DELETE FROM persona_modes WHERE subject_id = %s", (subject_id,))


def _ensure_active_profile(conn, subject_id: UUID) -> None:
    row = fetch_one(
        conn,
        "SELECT id FROM personality_profiles WHERE subject_id = %s AND status = 'active' LIMIT 1",
        (subject_id,),
    )
    if row:
        return
    execute(
        conn,
        """
        INSERT INTO personality_profiles (id, subject_id, name, status, activated_at)
        VALUES (%s, %s, 'test', 'active', NOW())
        """,
        (uuid7(), subject_id),
    )


def _seed_mode_evidence(conn, auth) -> None:
    when = datetime(2025, 6, 15, tzinfo=timezone.utc)
    mode_snippets = {
        "Analyst": "Fictional Analyst mode compares evidence and proposes concrete next steps.",
        "Guide": "Fictional Guide mode explains context with patient, plain language.",
        "Mediator": "Fictional Mediator mode surfaces trade-offs between competing constraints.",
        "Explorer": "Fictional Explorer mode proposes reversible experiments and prototypes.",
    }
    memory_ids: list[UUID] = []
    for _mode, text in mode_snippets.items():
        memory_ids.append(_insert_dated_memory(conn, auth, content=text, when=when))
        _insert_authored(conn, auth, content=text, mode_hint=_mode.lower())

    _insert_draft_digest(
        conn,
        auth,
        memory_ids=memory_ids,
        modes_text="Four fictional modes: Analyst, Guide, Mediator, and Explorer.",
    )


def test_build_all_mode_cards_evidence_and_mediator_aspirational(ensure_db):
    apply_migrations()
    get_settings.cache_clear()
    auth = resolve_auth(f"Bearer {get_settings().bootstrap_token.get_secret_value()}")
    with connection() as conn:
        _seed_mode_evidence(conn, auth)
        conn.commit()

    cards = build_all_mode_cards(auth)
    assert len(cards) == 4
    assert [c.name for c in cards] == list(CANONICAL_MODES)
    for card in cards:
        assert card.card_version == MODE_CARD_VERSION
        validate_mode_card(card)
    zara = next(c for c in cards if c.name == "Mediator")
    assert zara.status == "aspirational"


def test_propose_mode_cards_pending_with_persona_modes(ensure_db, monkeypatch):
    apply_migrations()
    get_settings.cache_clear()
    auth = resolve_auth(f"Bearer {get_settings().bootstrap_token.get_secret_value()}")

    with connection() as conn:
        _ensure_active_profile(conn, auth.subject_id)
        _cleanup_mode_proposals(conn, auth.subject_id)
        _seed_mode_evidence(conn, auth)
        conn.commit()

    monkeypatch.setattr(
        "virtual_self.services.mode_cards.notify_personality_proposal",
        lambda **kwargs: True,
    )

    out = propose_mode_cards(auth, dry_run=False, notify=True)
    assert out["status"] == "pending"
    assert set(out["proposals"].keys()) == set(CANONICAL_MODES)

    with transaction() as conn:
        for mode in CANONICAL_MODES:
            prop = fetch_one(
                conn,
                """
                SELECT pp.status, pp.origin_mode, pp.facet_type
                FROM personality_proposals pp
                WHERE pp.id = %s
                """,
                (out["proposals"][mode],),
            )
            assert prop["status"] == "pending"
            assert prop["origin_mode"] == mode
            assert prop["facet_type"] == f"mode_{mode.lower()}"
            mode_row = fetch_one(
                conn,
                "SELECT id, status FROM persona_modes WHERE subject_id = %s AND name = %s",
                (auth.subject_id, mode),
            )
            assert mode_row is not None
            if mode == "Mediator":
                assert mode_row["status"] == "aspirational"
            ev = fetch_one(
                conn,
                """
                SELECT COUNT(*) AS n FROM personality_proposal_evidence
                WHERE proposal_id = %s
                """,
                (out["proposals"][mode],),
            )
            assert int(ev["n"]) >= 3

    with pytest.raises(ConflictError):
        propose_mode_cards(auth, dry_run=False, notify=False)


def test_approve_mode_proposal_activates_draft_version(ensure_db, monkeypatch):
    apply_migrations()
    get_settings.cache_clear()
    auth = resolve_auth(f"Bearer {get_settings().bootstrap_token.get_secret_value()}")

    with connection() as conn:
        _ensure_active_profile(conn, auth.subject_id)
        _cleanup_mode_proposals(conn, auth.subject_id)
        _seed_mode_evidence(conn, auth)
        conn.commit()

    monkeypatch.setattr(
        "virtual_self.services.mode_cards.notify_personality_proposal",
        lambda **kwargs: True,
    )
    out = propose_mode_cards(auth, dry_run=False, notify=False)
    pid = out["proposals"]["Analyst"]

    result = approve_personality_proposal(auth, UUID(pid))
    assert result["status"] == "approved"
    assert result["mode_apply"]["mode"] == "Analyst"

    with transaction() as conn:
        ver = fetch_one(
            conn,
            """
            SELECT v.status
            FROM persona_mode_versions v
            JOIN persona_modes m ON m.id = v.mode_id
            WHERE m.subject_id = %s AND m.name = 'Analyst' AND v.id = m.current_version_id
            """,
            (auth.subject_id,),
        )
        assert ver["status"] == "active"
