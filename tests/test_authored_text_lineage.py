from __future__ import annotations

import hashlib
from uuid import uuid4

from uuid6 import uuid7

from virtual_self.auth.credentials import resolve_auth
from virtual_self.config import get_settings
from virtual_self.persistence.db import connection, fetch_one
from virtual_self.persistence.migrate import apply_migrations
from virtual_self.services import suppression
from virtual_self.services.authored_text_lineage import (
    AUTHORED_TEXT_SUPPRESSION_KIND,
    remove_authored_text_lineage,
)


def _fixture_ids():
    body = f"lineage cascade fixture {uuid4()}"
    text_hash = hashlib.sha256(body.encode()).hexdigest()
    return body, text_hash


def test_delete_authored_text_cascades_exemplar_and_facet_flags(ensure_db):
    apply_migrations()
    get_settings.cache_clear()
    auth = resolve_auth(f"Bearer {get_settings().bootstrap_token.get_secret_value()}")
    body, text_hash = _fixture_ids()
    fixture_register = "journal"
    authored_id = uuid7()
    profile_id = uuid7()
    facet_id = uuid7()
    facet_ver_id = uuid7()
    proposal_id = uuid7()
    style_id = uuid7()
    exemplar_id = uuid7()

    with connection() as conn:
        conn.execute(
            """
            INSERT INTO personality_profiles (id, subject_id, name, version, status)
            VALUES (%s, %s, 'lineage smoke', 1, 'draft')
            """,
            (profile_id, auth.subject_id),
        )
        conn.execute(
            """
            INSERT INTO personality_facets (id, profile_id, facet_type, mutability)
            VALUES (%s, %s, 'values', 'approval_required')
            """,
            (facet_id, profile_id),
        )
        conn.execute(
            """
            INSERT INTO personality_facet_versions (
              id, facet_id, version, content, priority, confidence, status
            ) VALUES (%s, %s, 1, 'values backed by fixture', 1, 0.8, 'active')
            """,
            (facet_ver_id, facet_id),
        )
        conn.execute(
            "UPDATE personality_facets SET current_version_id = %s WHERE id = %s",
            (facet_ver_id, facet_id),
        )
        conn.execute(
            """
            INSERT INTO authored_texts (
              id, subject_id, source_kind, author, register, content,
              word_count, text_hash
            ) VALUES (%s, %s, 'manual', 'self', %s, %s, 6, %s)
            """,
            (authored_id, auth.subject_id, fixture_register, body, text_hash),
        )
        conn.execute(
            """
            INSERT INTO personality_facet_evidence (
              id, facet_version_id, authored_text_id, relationship
            ) VALUES (%s, %s, %s, 'supports')
            """,
            (uuid7(), facet_ver_id, authored_id),
        )
        conn.execute(
            """
            INSERT INTO personality_proposals (
              id, profile_id, facet_type, proposed_content, origin_mode, status
            ) VALUES (%s, %s, 'values', 'fixture-backed sentence', 'Self', 'pending')
            """,
            (proposal_id, profile_id),
        )
        conn.execute(
            """
            INSERT INTO personality_proposal_evidence (
              id, proposal_id, authored_text_id, relationship
            ) VALUES (%s, %s, %s, 'supports')
            """,
            (uuid7(), proposal_id, authored_id),
        )
        conn.execute(
            """
            INSERT INTO style_profiles (
              id, subject_id, profile_id, register, version, features, n_texts, n_words
            ) VALUES (%s, %s, %s, %s, 1, '{}', 1, 6)
            """,
            (style_id, auth.subject_id, profile_id, fixture_register),
        )
        conn.execute(
            """
            INSERT INTO voice_exemplars (
              id, subject_id, authored_text_id, context, response, register, status
            ) VALUES (
              %s, %s, %s, '{"text": "prompt?", "context_synthetic": true}',
              %s, %s, 'approved'
            )
            """,
            (exemplar_id, auth.subject_id, authored_id, body, fixture_register),
        )
        conn.commit()

    out = remove_authored_text_lineage(auth, authored_id, mode="delete")
    assert out["voice_exemplars_removed"] == 1
    assert out["facet_versions_flagged"] == 1
    assert out["proposals_flagged"] == 1
    assert out["style_profiles_flagged"] == 1

    with connection() as conn:
        gone = fetch_one(
            conn,
            "SELECT id FROM authored_texts WHERE id = %s",
            (authored_id,),
        )
        assert gone is None
        ex = fetch_one(
            conn,
            "SELECT id FROM voice_exemplars WHERE id = %s",
            (exemplar_id,),
        )
        assert ex is None
        facet = fetch_one(
            conn,
            "SELECT needs_review FROM personality_facet_versions WHERE id = %s",
            (facet_ver_id,),
        )
        assert facet is not None and facet["needs_review"] is True
        prop = fetch_one(
            conn,
            "SELECT needs_review FROM personality_proposals WHERE id = %s",
            (proposal_id,),
        )
        assert prop is not None and prop["needs_review"] is True
        style = fetch_one(
            conn,
            "SELECT recompute_pending FROM style_profiles WHERE id = %s",
            (style_id,),
        )
        assert style is not None and style["recompute_pending"] is True
        assert suppression.is_suppressed(conn, auth.subject_id, AUTHORED_TEXT_SUPPRESSION_KIND, body)


def test_suppress_authored_text_keeps_row_and_records_suppression(ensure_db):
    apply_migrations()
    get_settings.cache_clear()
    auth = resolve_auth(f"Bearer {get_settings().bootstrap_token.get_secret_value()}")
    body, text_hash = _fixture_ids()
    authored_id = uuid7()

    with connection() as conn:
        conn.execute(
            """
            INSERT INTO authored_texts (
              id, subject_id, source_kind, author, register, content,
              word_count, text_hash
            ) VALUES (%s, %s, 'manual', 'self', 'journal', %s, 6, %s)
            """,
            (authored_id, auth.subject_id, body, text_hash),
        )
        conn.commit()

    remove_authored_text_lineage(auth, authored_id, mode="suppress")

    with connection() as conn:
        row = fetch_one(
            conn,
            """
            SELECT excluded_reason, invalidated_at
            FROM authored_texts WHERE id = %s
            """,
            (authored_id,),
        )
        assert row is not None
        assert row["excluded_reason"] == "user_suppressed"
        assert row["invalidated_at"] is not None
        assert suppression.is_suppressed(conn, auth.subject_id, AUTHORED_TEXT_SUPPRESSION_KIND, body)
