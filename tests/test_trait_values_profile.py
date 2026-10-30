from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from uuid6 import uuid7

from virtual_self.auth.credentials import resolve_auth
from virtual_self.config import get_settings
from virtual_self.domain.errors import ConflictError
from virtual_self.persistence.db import connection, execute, fetch_one, transaction
from virtual_self.persistence.migrate import apply_migrations
from virtual_self.services.proposal_review import approve_personality_proposal
from virtual_self.services.trait_values_profile import (
    PROFILE_VERSION,
    build_trait_values_profile,
    propose_trait_values_profile,
    validate_trait_values_profile,
)


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


def _insert_draft_digest(conn, auth, *, memory_id: UUID, title: str = "Era A") -> UUID:
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
            title,
            "chapter",
            datetime(2024, 6, 1, tzinfo=timezone.utc),
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

    layers = [
        ("values", "I treat honesty as non-negotiable even when it slows projects."),
        ("decision_style", "Under uncertainty I gather evidence before committing."),
        ("voice", "When exhausted I write shorter, blunter journal entries."),
        ("modes", "Integrated self voice shows up as direct planning language."),
    ]
    sort_order = 0
    for layer, text in layers:
        item_id = uuid7()
        execute(
            conn,
            """
            INSERT INTO life_chapter_guided_digest_items (
              id, digest_id, step, layer, text, stance, sort_order
            ) VALUES (%s, %s, 'profile_sentence', %s, %s, 'actual', %s)
            """,
            (item_id, digest_id, layer, text, sort_order),
        )
        execute(
            conn,
            """
            INSERT INTO life_chapter_guided_digest_evidence (
              id, item_id, memory_id, relationship
            ) VALUES (%s, %s, %s, 'supports')
            """,
            (uuid7(), item_id, memory_id),
        )
        sort_order += 1
    return digest_id


def _cleanup_trait_proposals(conn, subject_id: UUID) -> None:
    conn.execute(
        """
        DELETE FROM personality_proposal_evidence
        WHERE proposal_id IN (
          SELECT pp.id FROM personality_proposals pp
          JOIN personality_profiles prof ON prof.id = pp.profile_id
          WHERE prof.subject_id = %s
            AND pp.facet_type IN ('traits', 'values', 'interaction_preferences')
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
          AND pp.facet_type IN ('traits', 'values', 'interaction_preferences')
        """,
        (subject_id,),
    )


def _ensure_active_profile(conn, subject_id: UUID) -> None:
    row = fetch_one(
        conn,
        "SELECT id FROM personality_profiles WHERE subject_id = %s AND status = 'active' LIMIT 1",
        (subject_id,),
    )
    if row:
        return
    pid = uuid7()
    execute(
        conn,
        """
        INSERT INTO personality_profiles (id, subject_id, name, status, activated_at)
        VALUES (%s, %s, 'test', 'active', NOW())
        """,
        (pid, subject_id),
    )


def test_build_trait_values_profile_from_digest(ensure_db, tmp_path: Path):
    apply_migrations()
    get_settings.cache_clear()
    auth = resolve_auth(f"Bearer {get_settings().bootstrap_token.get_secret_value()}")
    ipip = tmp_path / "ipip.json"
    ipip.write_text(
        json.dumps({"facet_order_30": [2.0] * 30}),
        encoding="utf-8",
    )
    with connection() as conn:
        mem_id = _insert_dated_memory(
            conn,
            auth,
            content="Honesty beats speed when shipping homelab changes.",
            when=datetime(2024, 6, 2, tzinfo=timezone.utc),
        )
        _insert_draft_digest(conn, auth, memory_id=mem_id)

    profile = build_trait_values_profile(auth, ipip_path=ipip)
    assert profile.profile_version == PROFILE_VERSION
    assert profile.digest_count >= 1
    assert len(profile.traits) == 5
    assert profile.values
    validate_trait_values_profile(profile)
    assert "no scores exported" in profile.ipip_calibration_note.lower()


def test_propose_trait_values_pending_not_active(ensure_db, monkeypatch, tmp_path: Path):
    apply_migrations()
    get_settings.cache_clear()
    auth = resolve_auth(f"Bearer {get_settings().bootstrap_token.get_secret_value()}")
    ipip = tmp_path / "ipip.json"
    ipip.write_text(json.dumps({"facet_order_30": [3.0] * 30}), encoding="utf-8")

    with connection() as conn:
        _ensure_active_profile(conn, auth.subject_id)
        _cleanup_trait_proposals(conn, auth.subject_id)
        mem_id = _insert_dated_memory(
            conn,
            auth,
            content="I praise craftsmanship in my own notes when a build finally works.",
            when=datetime(2024, 7, 1, tzinfo=timezone.utc),
        )
        _insert_draft_digest(conn, auth, memory_id=mem_id, title="Build era")

    monkeypatch.setattr(
        "virtual_self.services.trait_values_profile.IPIP_PRIVATE_PATH",
        ipip,
    )
    monkeypatch.setattr(
        "virtual_self.services.trait_values_profile.notify_personality_proposal",
        lambda **kwargs: True,
    )

    with transaction() as conn:
        before = fetch_one(
            conn,
            """
            SELECT v.content, v.version
            FROM personality_facets f
            JOIN personality_facet_versions v ON v.id = f.current_version_id
            JOIN personality_profiles p ON p.id = f.profile_id
            WHERE p.subject_id = %s AND f.facet_type = 'values'
            """,
            (auth.subject_id,),
        )

    out = propose_trait_values_profile(auth, dry_run=False, notify=True)
    assert out["status"] == "pending"
    assert set(out["proposals"].keys()) == {"traits", "values", "interaction_preferences"}

    with transaction() as conn:
        traits_prop = fetch_one(
            conn,
            "SELECT status, origin_mode FROM personality_proposals WHERE id = %s",
            (out["proposals"]["traits"],),
        )
        ev = fetch_one(
            conn,
            """
            SELECT COUNT(*) AS n FROM personality_proposal_evidence
            WHERE proposal_id = %s
            """,
            (out["proposals"]["traits"],),
        )
        facet = fetch_one(
            conn,
            """
            SELECT v.content, v.version
            FROM personality_facets f
            JOIN personality_facet_versions v ON v.id = f.current_version_id
            JOIN personality_profiles p ON p.id = f.profile_id
            WHERE p.subject_id = %s AND f.facet_type = 'values'
            """,
            (auth.subject_id,),
        )

    assert traits_prop["status"] == "pending"
    assert traits_prop["origin_mode"] == "Self"
    assert int(ev["n"]) > 0
    if before:
        assert facet["content"] == before["content"]
        assert facet["version"] == before["version"]

    with pytest.raises(ConflictError):
        propose_trait_values_profile(auth, dry_run=False, notify=False)


def test_approve_traits_proposal_bumps_facet(ensure_db, monkeypatch, tmp_path: Path):
    apply_migrations()
    get_settings.cache_clear()
    auth = resolve_auth(f"Bearer {get_settings().bootstrap_token.get_secret_value()}")
    ipip = tmp_path / "ipip.json"
    ipip.write_text(json.dumps({"facet_order_30": [2.5] * 30}), encoding="utf-8")

    with connection() as conn:
        _ensure_active_profile(conn, auth.subject_id)
        _cleanup_trait_proposals(conn, auth.subject_id)
        mem_id = _insert_dated_memory(
            conn,
            auth,
            content="I disagree with authority when the reasoning is hand-wavy.",
            when=datetime(2024, 8, 1, tzinfo=timezone.utc),
        )
        _insert_draft_digest(conn, auth, memory_id=mem_id)

    monkeypatch.setattr("virtual_self.services.trait_values_profile.IPIP_PRIVATE_PATH", ipip)
    monkeypatch.setattr(
        "virtual_self.services.trait_values_profile.notify_personality_proposal",
        lambda **kwargs: True,
    )
    out = propose_trait_values_profile(auth, dry_run=False, notify=False)
    pid = out["proposals"]["traits"]

    result = approve_personality_proposal(auth, __import__("uuid").UUID(pid))
    assert result["status"] == "approved"
    assert result["traits_apply"]["facet_version"] >= 1

    with transaction() as conn:
        facet = fetch_one(
            conn,
            """
            SELECT v.content, v.version, v.status
            FROM personality_facets f
            JOIN personality_facet_versions v ON v.id = f.current_version_id
            JOIN personality_profiles p ON p.id = f.profile_id
            WHERE p.subject_id = %s AND f.facet_type = 'traits'
            """,
            (auth.subject_id,),
        )
        ev = fetch_one(
            conn,
            """
            SELECT COUNT(*) AS n
            FROM personality_facet_evidence fe
            JOIN personality_facet_versions fv ON fv.id = fe.facet_version_id
            JOIN personality_facets pf ON pf.id = fv.facet_id
            JOIN personality_profiles pp ON pp.id = pf.profile_id
            WHERE pp.subject_id = %s AND pf.facet_type = 'traits'
            """,
            (auth.subject_id,),
        )

    assert facet["status"] == "active"
    assert "Big Five" in facet["content"] or "openness" in facet["content"].lower()
    assert int(ev["n"]) > 0
