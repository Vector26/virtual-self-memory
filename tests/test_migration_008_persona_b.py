from __future__ import annotations

import hashlib
from uuid import uuid4

from uuid6 import uuid7

from virtual_self.auth.credentials import resolve_auth
from virtual_self.config import get_settings
from virtual_self.persistence.db import connection, fetch_one
from virtual_self.persistence.migrate import apply_migrations

EXPECTED_TABLES = {
    "persona_modes",
    "persona_mode_versions",
    "persona_mode_evidence",
    "personality_facet_evidence",
    "personality_proposals",
    "personality_proposal_evidence",
    "style_profiles",
    "voice_exemplars",
    "anti_patterns",
    "persona_eval_runs",
    "persona_eval_results",
}


def test_migration_008_persona_b_idempotent_and_constraints(ensure_db):
    apply_migrations()
    apply_migrations()

    with connection() as conn:
        tables = {
            r["table_name"]
            for r in conn.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = current_schema()
                  AND table_name = ANY(%s)
                """,
                (list(EXPECTED_TABLES),),
            ).fetchall()
        }
    assert tables == EXPECTED_TABLES

    get_settings.cache_clear()
    token = get_settings().bootstrap_token
    auth = resolve_auth(f"Bearer {token.get_secret_value()}")

    body = f"migration 008-b smoke corpus line {uuid4()}"
    text_hash = hashlib.sha256(body.encode()).hexdigest()
    authored_id = uuid7()
    profile_id = uuid7()
    facet_id = uuid7()
    facet_ver_id = uuid7()
    mode_name = f"Default-{uuid4().hex[:8]}"
    mode_id = uuid7()
    mode_ver_id = uuid7()
    proposal_id = uuid7()
    style_id = uuid7()
    exemplar_id = uuid7()
    anti_id = uuid7()
    eval_run_id = uuid7()
    eval_result_id = uuid7()

    with connection() as conn:
        conn.execute(
            """
            INSERT INTO personality_profiles (id, subject_id, name, version, status)
            VALUES (%s, %s, 'smoke persona b', 1, 'draft')
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
            ) VALUES (%s, %s, 1, 'smoke values facet', 1, 0.8, 'active')
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
            ) VALUES (%s, %s, 'manual', 'self', 'notes', %s, 6, %s)
            """,
            (authored_id, auth.subject_id, body, text_hash),
        )
        conn.execute(
            """
            INSERT INTO persona_modes (
              id, subject_id, name, role_summary, status
            ) VALUES (%s, %s, %s, 'id voice smoke', 'active')
            """,
            (mode_id, auth.subject_id, mode_name),
        )
        conn.execute(
            """
            INSERT INTO persona_mode_versions (
              id, mode_id, version, role_summary, status
            ) VALUES (%s, %s, 1, 'id voice smoke', 'active')
            """,
            (mode_ver_id, mode_id),
        )
        conn.execute(
            "UPDATE persona_modes SET current_version_id = %s WHERE id = %s",
            (mode_ver_id, mode_id),
        )
        conn.execute(
            """
            INSERT INTO persona_mode_evidence (
              id, mode_version_id, authored_text_id, relationship
            ) VALUES (%s, %s, %s, 'supports')
            """,
            (uuid7(), mode_ver_id, authored_id),
        )
        conn.execute(
            """
            INSERT INTO personality_facet_evidence (
              id, facet_version_id, authored_text_id, relationship
            ) VALUES (%s, %s, %s, 'illustrates')
            """,
            (uuid7(), facet_ver_id, authored_id),
        )
        conn.execute(
            """
            INSERT INTO personality_proposals (
              id, profile_id, facet_type, proposed_content, origin_mode, status
            ) VALUES (%s, %s, 'values', 'authority must earn trust', 'Default', 'pending')
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
            ) VALUES (%s, %s, %s, 'notes', 1, '{"smoke": true}', 1, 6)
            """,
            (style_id, auth.subject_id, profile_id),
        )
        conn.execute(
            """
            INSERT INTO voice_exemplars (
              id, subject_id, authored_text_id, context, response, register, mode, status
            ) VALUES (
              %s, %s, %s, '{"text": "what is on your mind?", "context_synthetic": true}',
              %s, 'notes', 'Default', 'approved'
            )
            """,
            (exemplar_id, auth.subject_id, authored_id, body),
        )
        conn.execute(
            """
            INSERT INTO anti_patterns (id, subject_id, pattern, kind, origin, severity)
            VALUES (%s, %s, 'as an ai language model', 'phrase', 'seed_slop_list', 3)
            ON CONFLICT (subject_id, pattern, kind) DO NOTHING
            """,
            (anti_id, auth.subject_id),
        )
        conn.execute(
            """
            INSERT INTO persona_eval_runs (
              id, subject_id, suite_version, profile_version, composer_version, model_id, metrics
            ) VALUES (%s, %s, 'persona_eval_v0', 1, 'composer-smoke', 'test-model', '{"ok": true}')
            """,
            (eval_run_id, auth.subject_id),
        )
        conn.execute(
            """
            INSERT INTO persona_eval_results (
              id, run_id, case_id, scenario, output, metrics, judge_id
            ) VALUES (%s, %s, 'case-smoke-1', 'notes register', 'smoke output', '{}', 'deterministic')
            """,
            (eval_result_id, eval_run_id),
        )
        dup = conn.execute(
            """
            INSERT INTO style_profiles (
              id, subject_id, profile_id, register, version, features
            ) VALUES (%s, %s, %s, 'notes', 1, '{}')
            ON CONFLICT (profile_id, register, version) DO NOTHING
            """,
            (uuid7(), auth.subject_id, profile_id),
        )
        assert dup.rowcount == 0
        row = fetch_one(
            conn,
            "SELECT origin_mode FROM personality_proposals WHERE id = %s",
            (proposal_id,),
        )
        conn.commit()

    assert row is not None
    assert row["origin_mode"] == "Default"
