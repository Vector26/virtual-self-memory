from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from uuid6 import uuid7

from virtual_self.auth.credentials import AuthContext
from virtual_self.domain.errors import NotFoundError
from virtual_self.eval.heldout_baseline import heldout_stylometry_baselines
from virtual_self.eval.register_stylometry import (
    MIN_REGISTER_WORDS,
    PRIMARY_REGISTERS,
    aggregate_register_profile,
)
from virtual_self.persistence.db import connection, fetch_one, transaction

STYLE_PROFILE_VERSION = 1
STYLE_FINGERPRINT_DOC = Path(__file__).resolve().parents[3] / "docs" / "STYLE_FINGERPRINT.md"


def load_train_texts_by_register(subject_id: UUID, register: str) -> list[str]:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT content
            FROM authored_texts
            WHERE subject_id = %s
              AND author = 'self'
              AND split = 'train'
              AND register = %s
            ORDER BY authored_at NULLS LAST, id
            """,
            (subject_id, register),
        ).fetchall()
    return [str(r["content"]) for r in rows if r.get("content")]


def _active_profile_id(conn, subject_id: UUID) -> UUID:
    row = fetch_one(
        conn,
        """
        SELECT id FROM personality_profiles
        WHERE subject_id = %s AND status = 'active'
        ORDER BY activated_at DESC NULLS LAST
        LIMIT 1
        """,
        (subject_id,),
    )
    if not row:
        raise NotFoundError("no active personality profile — bootstrap personality first")
    return row["id"]


def build_register_profiles(
    subject_id: UUID,
    *,
    registers: tuple[str, ...] = PRIMARY_REGISTERS,
) -> dict[str, Any]:
    heldout = heldout_stylometry_baselines()
    profiles: dict[str, Any] = {}
    for register in registers:
        texts = load_train_texts_by_register(subject_id, register)
        features = aggregate_register_profile(texts)
        features["heldout_eval_baseline"] = heldout.get(register)
        features["source_window"] = {
            "split": "train",
            "heldout_excluded": True,
            "source_kinds": ["notion"],
            "min_register_words": MIN_REGISTER_WORDS,
        }
        profiles[register] = features
    return profiles


def persist_style_profiles(
    auth: AuthContext,
    profiles: dict[str, Any],
    *,
    version: int = STYLE_PROFILE_VERSION,
) -> dict[str, Any]:
    written: list[str] = []
    profile_id: UUID
    with transaction() as conn:
        profile_id = _active_profile_id(conn, auth.subject_id)
        for register, features in profiles.items():
            row_id = uuid7()
            source_window = features.get("source_window") or {}
            conn.execute(
                """
                INSERT INTO style_profiles (
                  id, subject_id, profile_id, register, version,
                  features, n_texts, n_words, source_window
                ) VALUES (
                  %s, %s, %s, %s, %s,
                  %s::jsonb, %s, %s, %s::jsonb
                )
                ON CONFLICT (profile_id, register, version) DO UPDATE SET
                  features = EXCLUDED.features,
                  n_texts = EXCLUDED.n_texts,
                  n_words = EXCLUDED.n_words,
                  source_window = EXCLUDED.source_window,
                  created_at = NOW()
                """,
                (
                    row_id,
                    auth.subject_id,
                    profile_id,
                    register,
                    version,
                    json.dumps(features),
                    int(features.get("n_texts") or 0),
                    int(features.get("n_words") or 0),
                    json.dumps(source_window),
                ),
            )
            written.append(register)

    return {
        "profile_id": str(profile_id),
        "version": version,
        "registers": written,
        "subject_id": str(auth.subject_id),
    }


def render_style_fingerprint_md(profiles: dict[str, Any], *, generated_at: str | None = None) -> str:
    ts = generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "# Style fingerprint (per register)",
        "",
        f"Generated: `{ts}` — CPU stylometry from **train-split** `authored_texts` (held-out excluded).",
        "Orthographic metrics are measured but **not for imitation** (clean-typing decision).",
        "",
    ]
    for register in PRIMARY_REGISTERS:
        feat = profiles.get(register) or {}
        lines.append(f"## {register}")
        lines.append("")
        if not feat.get("sufficient"):
            reason = feat.get("insufficient_reason") or "insufficient corpus"
            lines.append(f"- **Status:** insufficient — {reason}")
            lines.append(f"- Train texts: {feat.get('n_texts', 0)}, words: {feat.get('n_words', 0)}")
            lines.append("")
            continue

        length = feat.get("length") or {}
        wpm = (length.get("words_per_message") or {}).get("mean")
        lines.append(f"- **Status:** sufficient ({feat.get('n_words')} train words, {feat.get('n_texts')} texts)")
        lines.append(f"- **Length:** ~{wpm} words/message (mean)")
        lex = feat.get("lexicon") or {}
        punct = feat.get("punctuation_per_100_words") or {}
        md = feat.get("markdown_per_100_words") or {}
        lines.append(
            f"- **Punctuation / 100w:** periods {punct.get('periods_per_100_words')}, "
            f"questions {punct.get('questions_per_100_words')}, commas {punct.get('commas_per_100_words')}"
        )
        lines.append(
            f"- **Markdown / 100w:** headers {md.get('markdown_headers_per_100_words')}, "
            f"bold {md.get('markdown_bold_per_100_words')}, bullets {md.get('markdown_bullets_per_100_words')}"
        )
        lines.append(
            f"- **Lexicon:** hedges {lex.get('hedges_per_100_words')}/100w, "
            f"emoji {lex.get('emoji_per_100_words')}/100w, profanity {lex.get('profanity_per_100_words')}/100w"
        )
        lines.append(
            f"- **Questions:** rate {feat.get('question_rate')}, closer rate {feat.get('question_closer_rate')}"
        )
        distinctive = lex.get("distinctive_words") or []
        if distinctive:
            top = ", ".join(f"`{d['word']}`" for d in distinctive[:8])
            lines.append(f"- **Distinctive words:** {top}")
        pets = lex.get("pet_phrases") or []
        if pets:
            top_p = ", ".join(f'"{p["phrase"]}" ({p["count"]})' for p in pets[:3])
            lines.append(f"- **Pet phrases (bigrams):** {top_p}")
        fw = feat.get("function_word_frequencies") or {}
        if fw:
            top_fw = sorted(fw.items(), key=lambda kv: kv[1], reverse=True)[:5]
            lines.append(
                "- **Function-word vector (Burrows-ready):** "
                + ", ".join(f"{w}={v:.4f}" for w, v in top_fw)
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_style_fingerprint_doc(profiles: dict[str, Any], path: Path | None = None) -> Path:
    target = path or STYLE_FINGERPRINT_DOC
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_style_fingerprint_md(profiles), encoding="utf-8")
    return target


def run_build_style_profiles(auth: AuthContext, *, doc_path: Path | None = None) -> dict[str, Any]:
    profiles = build_register_profiles(auth.subject_id)
    persist = persist_style_profiles(auth, profiles)
    doc = write_style_fingerprint_doc(profiles, doc_path)
    return {
        **persist,
        "doc_path": str(doc),
        "profiles": {k: {"sufficient": v.get("sufficient"), "n_words": v.get("n_words")} for k, v in profiles.items()},
    }
