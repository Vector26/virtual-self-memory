"""Voice exemplar bank (V-3): train-split self texts + slop anti-pattern sync."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from uuid6 import uuid7

from virtual_self.auth.credentials import AuthContext
from virtual_self.eval.register_norms import register_word_norm
from virtual_self.eval.register_stylometry import PRIMARY_REGISTERS, extract_text_features
from virtual_self.eval.slop import check_slop
from virtual_self.eval.slop_seeds import all_anti_pattern_seeds
from virtual_self.persistence.db import connection, fetch_all, fetch_one, transaction
from virtual_self.providers.embeddings import embed_texts, vector_literal
from virtual_self.services.llm_likeness_pass import EXCLUDED_REASON as LLM_EXCLUDED
from virtual_self.services.style_profiles import STYLE_PROFILE_VERSION

VOICE_EXEMPLAR_AUDIT = Path(__file__).resolve().parents[3] / ".state" / "voice_exemplars_build.json"

MIN_RESPONSE_WORDS = 20
MAX_PER_REGISTER = 60
TARGET_CANDIDATES = 100
SENSITIVITY_MAX = 2  # sensitivity < 3
_NEAR_DUPE_JACCARD = 0.92

_TOKEN = re.compile(r"[a-z']+", re.I)

_SYNTHETIC_CONTEXT: dict[str, str] = {
    "journal": "What's been on your mind lately?",
    "casual_chat": "Quick check-in — what's up?",
    "technical": "Walk me through how you'd approach this technically.",
    "work_formal": "Draft a concise work reply about this.",
    "social": "What's your take in a social thread?",
    "notes": "Capture this as a short note to self.",
}


@dataclass
class ExemplarBuildStats:
    eligible_rows: int = 0
    scored: int = 0
    selected: int = 0
    skipped_short: int = 0
    skipped_slop: int = 0
    skipped_near_dupe: int = 0
    by_register: dict[str, int] = field(default_factory=dict)
    anti_patterns_written: int = 0
    embeddings_set: int = 0
    embeddings_skipped: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible_rows": self.eligible_rows,
            "scored": self.scored,
            "selected": self.selected,
            "skipped_short": self.skipped_short,
            "skipped_slop": self.skipped_slop,
            "skipped_near_dupe": self.skipped_near_dupe,
            "by_register": dict(self.by_register),
            "anti_patterns_written": self.anti_patterns_written,
            "embeddings_set": self.embeddings_set,
            "embeddings_skipped": self.embeddings_skipped,
            "target_candidates": TARGET_CANDIDATES,
        }


@dataclass(frozen=True)
class _CandidateRow:
    id: UUID
    register: str
    content: str
    source_kind: str
    source_ref: dict[str, Any]
    mode_hint: str | None
    word_count: int
    text_hash: str


def _token_set(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


def _near_dupe_token_set(text: str) -> set[str]:
    """Word tokens with digits stripped — near-dupes, not exact-hash matches."""
    out: set[str] = set()
    for token in _TOKEN.findall(text.lower()):
        stripped = "".join(ch for ch in token if not ch.isdigit())
        if len(stripped) >= 2:
            out.add(stripped)
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _load_register_length_means(conn, subject_id: UUID) -> dict[str, float]:
    rows = fetch_all(
        conn,
        """
        SELECT DISTINCT ON (register) register, features
        FROM style_profiles
        WHERE subject_id = %s AND version = %s
        ORDER BY register, created_at DESC
        """,
        (subject_id, STYLE_PROFILE_VERSION),
    )
    out: dict[str, float] = {}
    for row in rows:
        feat = row["features"]
        if isinstance(feat, str):
            feat = json.loads(feat)
        length = (feat or {}).get("length") or {}
        wpm = (length.get("words_per_message") or {}).get("mean") or (length.get("words_per_message") or {}).get(
            "p50"
        )
        if wpm:
            out[str(row["register"])] = float(wpm)
    return out


def _chat_preceding_assistant(conn, source_ref: dict[str, Any]) -> str | None:
    session_id = source_ref.get("session_id")
    turn_index = source_ref.get("turn_index")
    if not session_id or turn_index is None:
        return None
    row = fetch_one(
        conn,
        """
        SELECT content
        FROM chat_turns
        WHERE session_id = %s AND role = 'assistant' AND turn_index < %s
        ORDER BY turn_index DESC
        LIMIT 1
        """,
        (UUID(str(session_id)), int(turn_index)),
    )
    if not row or not row.get("content"):
        return None
    return str(row["content"]).strip()


def _build_context(conn, row: _CandidateRow) -> dict[str, Any]:
    if row.source_kind == "chat_user_turn":
        prior = _chat_preceding_assistant(conn, row.source_ref)
        if prior:
            return {"text": prior[:4000], "context_synthetic": False, "source": "chat_assistant_turn"}
    prompt = _SYNTHETIC_CONTEXT.get(row.register) or _SYNTHETIC_CONTEXT["notes"]
    return {"text": prompt, "context_synthetic": True, "register": row.register}


def _topic_tags(content: str, *, limit: int = 4) -> list[str]:
    tokens = [t.lower() for t in _TOKEN.findall(content) if len(t) > 3]
    if not tokens:
        return []
    counts: dict[str, int] = {}
    for t in tokens:
        counts[t] = counts.get(t, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [w for w, _ in ranked[:limit]]


def quality_score(
    content: str,
    register: str,
    *,
    register_length_mean: float | None,
) -> float:
    text = content.strip()
    wc = extract_text_features(text).get("words", 0)
    if wc < MIN_RESPONSE_WORDS:
        return 0.0

    score = 0.0
    slop = check_slop(text[:4000], register=register)
    if slop.passed:
        score += 0.35

    norm = register_word_norm(register)
    if 0.35 * norm <= wc <= 1.8 * norm:
        score += 0.25
    elif wc <= 2.2 * norm:
        score += 0.1

    if register_length_mean and register_length_mean > 0:
        ratio = wc / register_length_mean
        if 0.5 <= ratio <= 1.6:
            score += 0.25
        elif 0.35 <= ratio <= 2.0:
            score += 0.1
    else:
        score += 0.15

    if wc >= max(MIN_RESPONSE_WORDS, int(norm * 0.4)):
        score += 0.15

    return round(min(1.0, score), 4)


def _load_eligible_rows(subject_id: UUID) -> list[_CandidateRow]:
    with connection() as conn:
        rows = fetch_all(
            conn,
            """
            SELECT id, register, content, source_kind, source_ref, mode_hint, word_count, text_hash
            FROM authored_texts
            WHERE subject_id = %s
              AND author = 'self'
              AND split = 'train'
              AND sensitivity <= %s
              AND excluded_reason IS DISTINCT FROM %s
              AND invalidated_at IS NULL
              AND word_count >= %s
            ORDER BY authored_at NULLS LAST, id
            """,
            (subject_id, SENSITIVITY_MAX, LLM_EXCLUDED, MIN_RESPONSE_WORDS),
        )
    out: list[_CandidateRow] = []
    for row in rows:
        ref = row["source_ref"]
        if isinstance(ref, str):
            ref = json.loads(ref)
        out.append(
            _CandidateRow(
                id=row["id"],
                register=str(row["register"]),
                content=str(row["content"]),
                source_kind=str(row["source_kind"]),
                source_ref=dict(ref or {}),
                mode_hint=str(row["mode_hint"]) if row.get("mode_hint") else None,
                word_count=int(row["word_count"]),
                text_hash=str(row["text_hash"]),
            )
        )
    return out


def _select_exemplars(
    rows: list[_CandidateRow],
    length_means: dict[str, float],
) -> tuple[list[tuple[_CandidateRow, float]], ExemplarBuildStats]:
    stats = ExemplarBuildStats(eligible_rows=len(rows))
    scored: list[tuple[_CandidateRow, float]] = []
    for row in rows:
        if row.word_count < MIN_RESPONSE_WORDS:
            stats.skipped_short += 1
            continue
        q = quality_score(row.content, row.register, register_length_mean=length_means.get(row.register))
        stats.scored += 1
        if q <= 0.05:
            stats.skipped_slop += 1
            continue
        scored.append((row, q))

    scored.sort(key=lambda pair: (-pair[1], -pair[0].word_count))

    selected: list[tuple[_CandidateRow, float]] = []
    kept_tokens: dict[str, list[set[str]]] = {reg: [] for reg in PRIMARY_REGISTERS}
    kept_tokens.update({r: [] for r in {row.register for row, _ in scored}})
    seen_hashes: set[str] = set()

    per_reg_count: dict[str, int] = {}
    for row, q in scored:
        reg = row.register
        if per_reg_count.get(reg, 0) >= MAX_PER_REGISTER:
            continue
        if len(selected) >= max(TARGET_CANDIDATES, MAX_PER_REGISTER * len(PRIMARY_REGISTERS)):
            break
        if row.text_hash in seen_hashes:
            stats.skipped_near_dupe += 1
            continue

        tokens = _near_dupe_token_set(row.content)
        dup = False
        for prior in kept_tokens.setdefault(reg, []):
            if _jaccard(tokens, prior) >= _NEAR_DUPE_JACCARD:
                dup = True
                break
        if dup:
            stats.skipped_near_dupe += 1
            continue

        kept_tokens[reg].append(tokens)
        seen_hashes.add(row.text_hash)
        selected.append((row, q))
        per_reg_count[reg] = per_reg_count.get(reg, 0) + 1
        stats.by_register[reg] = stats.by_register.get(reg, 0) + 1

    stats.selected = len(selected)
    return selected, stats


def sync_anti_patterns_from_slop_baseline(
    auth: AuthContext,
    *,
    persona_eval_path: Path | None = None,
) -> int:
    written = 0
    with transaction() as conn:
        for pattern, kind, severity, origin in all_anti_pattern_seeds(persona_eval_path=persona_eval_path):
            conn.execute(
                """
                INSERT INTO anti_patterns (id, subject_id, pattern, kind, origin, severity)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (subject_id, pattern, kind) DO UPDATE SET
                  severity = GREATEST(anti_patterns.severity, EXCLUDED.severity),
                  origin = CASE
                    WHEN anti_patterns.origin = 'user_correction' THEN anti_patterns.origin
                    WHEN EXCLUDED.origin = 'eval_finding' AND anti_patterns.origin = 'seed_slop_list'
                      THEN 'eval_finding'
                    ELSE anti_patterns.origin
                  END
                """,
                (uuid7(), auth.subject_id, pattern, kind, origin, severity),
            )
            written += 1
    return written


def run_build_voice_exemplars(
    auth: AuthContext,
    *,
    dry_run: bool = False,
    embed: bool = True,
    persona_eval_path: Path | None = None,
    audit_path: Path | None = None,
) -> dict[str, Any]:
    rows = _load_eligible_rows(auth.subject_id)
    with transaction() as conn:
        length_means = _load_register_length_means(conn, auth.subject_id)

    selected, stats = _select_exemplars(rows, length_means)

    embeddings: list[list[float] | None] = []
    if embed and selected and not dry_run:
        embeddings = embed_texts([row.content[:8000] for row, _ in selected])
    elif selected:
        embeddings = [None] * len(selected)

    if not dry_run:
        stats.anti_patterns_written = sync_anti_patterns_from_slop_baseline(
            auth, persona_eval_path=persona_eval_path
        )
        with transaction() as conn:
            conn.execute(
                """
                DELETE FROM voice_exemplars
                WHERE subject_id = %s AND status = 'candidate'
                """,
                (auth.subject_id,),
            )
            for idx, (row, q) in enumerate(selected):
                ctx = _build_context(conn, row)
                tags = _topic_tags(row.content)
                emb = embeddings[idx] if idx < len(embeddings) else None
                if emb:
                    stats.embeddings_set += 1
                    emb_sql = vector_literal(emb)
                    conn.execute(
                        """
                        INSERT INTO voice_exemplars (
                          id, subject_id, authored_text_id, context, response,
                          register, mode, topic_tags, quality_score, status, embedding
                        ) VALUES (
                          %s, %s, %s, %s::jsonb, %s,
                          %s, %s, %s, %s, 'candidate', %s::vector
                        )
                        """,
                        (
                            uuid7(),
                            auth.subject_id,
                            row.id,
                            json.dumps(ctx),
                            row.content,
                            row.register,
                            row.mode_hint,
                            tags,
                            q,
                            emb_sql,
                        ),
                    )
                else:
                    stats.embeddings_skipped += 1
                    conn.execute(
                        """
                        INSERT INTO voice_exemplars (
                          id, subject_id, authored_text_id, context, response,
                          register, mode, topic_tags, quality_score, status
                        ) VALUES (
                          %s, %s, %s, %s::jsonb, %s,
                          %s, %s, %s, %s, 'candidate'
                        )
                        """,
                        (
                            uuid7(),
                            auth.subject_id,
                            row.id,
                            json.dumps(ctx),
                            row.content,
                            row.register,
                            row.mode_hint,
                            tags,
                            q,
                        ),
                    )

    report = {
        "dry_run": dry_run,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "subject_id": str(auth.subject_id),
        **stats.to_dict(),
    }
    if dry_run:
        report["anti_patterns_would_sync"] = len(all_anti_pattern_seeds(persona_eval_path=persona_eval_path))

    target = audit_path or VOICE_EXEMPLAR_AUDIT
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2), encoding="utf-8")
        report["audit_path"] = str(target)

    return report
