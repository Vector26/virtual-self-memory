from __future__ import annotations

import json

import pytest

from virtual_self.auth.credentials import AuthContext
from virtual_self.config import get_settings
from virtual_self.eval.register_stylometry import (
    MIN_REGISTER_WORDS,
    aggregate_register_profile,
    extract_text_features,
)
from virtual_self.persistence.db import connection, fetch_one
from virtual_self.persistence.migrate import apply_migrations
from virtual_self.services.style_profiles import (
    build_register_profiles,
    render_style_fingerprint_md,
    run_build_style_profiles,
)


def test_extract_text_features_markdown_and_questions():
    text = "## Title\n\nHello world! Does this match your experience?\n"
    f = extract_text_features(text)
    assert f["words"] >= 5
    assert f["markdown_headers_per_100_words"] > 0
    assert f["question_closer_rate"] == 1.0


def test_aggregate_register_profile_insufficient():
    prof = aggregate_register_profile(["short note"])
    assert not prof["sufficient"]
    assert prof["n_words"] < MIN_REGISTER_WORDS


def test_aggregate_register_profile_sufficient_distribution():
    texts = ["One two three four five."] * 400
    prof = aggregate_register_profile(texts)
    assert prof["sufficient"]
    assert prof["n_words"] >= MIN_REGISTER_WORDS
    assert "function_word_frequencies" in prof
    assert prof["orthographic_not_for_imitation"] is True
    assert prof["length"]["words_per_message"]["p50"] > 0


def test_render_fingerprint_marks_insufficient():
    md = render_style_fingerprint_md(
        {
            "casual_chat": {
                "sufficient": False,
                "insufficient_reason": "only 0 train words (need >=2000)",
                "n_texts": 0,
                "n_words": 0,
            },
            "journal": {"sufficient": True, "n_texts": 10, "n_words": 5000, "length": {"words_per_message": {"mean": 120}}},
            "technical": {"sufficient": True, "n_texts": 5, "n_words": 4000, "length": {"words_per_message": {"mean": 80}}},
        }
    )
    assert "insufficient" in md
    assert "casual_chat" in md


def test_build_style_profiles_integration(ensure_db):
    from uuid6 import uuid7

    apply_migrations()
    get_settings.cache_clear()
    subject_id = uuid7()
    auth = AuthContext(
        actor_id=subject_id,
        subject_id=subject_id,
        scopes=frozenset({"context.read"}),
        sensitivity_max=5,
    )

    filler = " ".join(["word"] * 80) + "."
    with connection() as conn:
        conn.execute(
            "INSERT INTO subjects (id, display_name) VALUES (%s, %s)",
            (subject_id, "stylometry-integration"),
        )
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
        for register in ("journal", "technical"):
            for _ in range(30):
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
                    (uuid7(), auth.subject_id, register, filler, 80, uuid7().hex),
                )
        conn.commit()

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        doc = Path(tmp) / "STYLE_FINGERPRINT.md"
        out = run_build_style_profiles(auth, doc_path=doc)
        assert out["version"] == 1
        assert set(out["profiles"].keys()) >= {"journal", "technical", "casual_chat"}
        assert doc.is_file()

        with connection() as conn:
            rows = conn.execute(
                """
                SELECT register, n_words, features
                FROM style_profiles
                WHERE subject_id = %s AND version = 1
                ORDER BY register
                """,
                (auth.subject_id,),
            ).fetchall()

        by_reg = {r["register"]: r for r in rows}
        assert "journal" in by_reg
        assert "technical" in by_reg
        assert "casual_chat" in by_reg
        journal_feat = by_reg["journal"]["features"]
        if isinstance(journal_feat, str):
            journal_feat = json.loads(journal_feat)
        assert journal_feat.get("sufficient") is True
        casual_feat = by_reg["casual_chat"]["features"]
        if isinstance(casual_feat, str):
            casual_feat = json.loads(casual_feat)
        assert casual_feat.get("sufficient") is False

        profiles = build_register_profiles(auth.subject_id)
        md = render_style_fingerprint_md(profiles)
        assert "journal" in md
