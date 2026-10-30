from __future__ import annotations

import json

import pytest
from uuid6 import uuid7

from virtual_self.auth.credentials import resolve_auth
from virtual_self.config import get_settings
from virtual_self.eval.slop_seeds import all_anti_pattern_seeds
from virtual_self.persistence.db import connection, fetch_one
from virtual_self.persistence.migrate import apply_migrations
from virtual_self.services.voice_exemplars import (
    MIN_RESPONSE_WORDS,
    _CandidateRow,
    _select_exemplars,
    quality_score,
    run_build_voice_exemplars,
    sync_anti_patterns_from_slop_baseline,
)

_JOURNAL_TOPICS = (
    "the homelab rebuild",
    "a friendship that cooled off",
    "burnout after a release week",
    "learning to cook again",
    "money anxiety and rent",
    "a long walk that cleared my head",
)
_TECH_TOPICS = (
    "postgres backups on the NUC",
    "nginx TLS and cert rotation",
    "systemd unit ordering",
    "vector embeddings in prod",
    "compose healthchecks",
    "redis eviction tuning",
)
_CHAT_TOPICS = (
    "weekend plans",
    "a weird dream",
    "that server rack photo",
    "music recs",
    "gym consistency",
    "a half-finished side project",
)
_DETAIL_SUBJECTS = (
    "the attic boxes",
    "a thrifted keyboard",
    "the broken zipper",
    "rain on the window",
    "a missed call",
    "the sourdough starter",
    "a cracked phone screen",
    "the neighbor's cat",
    "an overdue library book",
    "the flickering monitor",
)
_DETAIL_VERBS = (
    "sorted",
    "ignored",
    "fixed",
    "photographed",
    "postponed",
    "donated",
    "labeled",
    "replayed",
    "sketched",
    "debated",
)
_DETAIL_OBJECTS = (
    "cables",
    "receipts",
    "voice memos",
    "calendar blocks",
    "browser tabs",
    "old tickets",
    "paint swatches",
    "seed packets",
    "workout logs",
    "chat exports",
)


def _lexical_salt(index: int) -> str:
    """Letter-only nonce words — digit-stripped Jaccard must not collapse distinct rows."""
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    chunks: list[str] = []
    n = index + 1
    for _ in range(10):
        a = alphabet[n % 26]
        b = alphabet[(n // 26) % 26]
        c = alphabet[(n // 676) % 26]
        chunks.append(f"{a}{b}{c}trail")
        n = n * 31 + 17
    return " ".join(chunks)


def _detail_sentence(index: int) -> str:
    """Lexically distinct middle sentence so near-dupe Jaccard stays below production threshold."""
    s = _DETAIL_SUBJECTS[index % len(_DETAIL_SUBJECTS)]
    v = _DETAIL_VERBS[(index // len(_DETAIL_SUBJECTS)) % len(_DETAIL_VERBS)]
    o = _DETAIL_OBJECTS[(index // (len(_DETAIL_SUBJECTS) * len(_DETAIL_VERBS))) % len(_DETAIL_OBJECTS)]
    return (
        f"I also {v} {s} and filed away {o} before midnight. "
        f"Private vocabulary for row {index}: {_lexical_salt(index)}."
    )


def _diverse_exemplar_prose(register: str, index: int) -> str:
    """Realistic train-split prose — unique per index, long enough for MIN_RESPONSE_WORDS."""
    detail = _detail_sentence(index)
    if register == "journal":
        topic = _JOURNAL_TOPICS[index % len(_JOURNAL_TOPICS)]
        return (
            f"Journal entry {index}: I keep circling {topic} and trying to name what changed. "
            f"{detail} "
            f"I am not looking for a pep talk, just an honest line about where I am tonight. "
            f"The homelab can wait; this note is for future me reading back."
        )
    if register == "technical":
        topic = _TECH_TOPICS[index % len(_TECH_TOPICS)]
        return (
            f"Technical note {index}: documenting {topic} so I do not re-debug blind next month. "
            f"{detail} "
            f"I changed one variable at a time, kept logs, and rolled back when the stack got weird. "
            f"Next step is a small runbook entry, not a markdown essay."
        )
    topic = _CHAT_TOPICS[index % len(_CHAT_TOPICS)]
    return (
        f"Casual message {index}: lol yeah {topic} — I am around tonight if you want to compare notes. "
        f"{detail} "
        f"Nothing fancy, just checking in after a long day. "
        f"We can keep it low-key; I will bring snacks."
    )


def _seed_train_rows(auth, *, register: str, count: int) -> None:
    with connection() as conn:
        for i in range(count):
            content = (
                f"Register {register} sample {i}: I wrote this train-split note about "
                f"cluster{i} service{i} pipeline{i} without assistant filler or markdown headers. "
                f"Keep it direct — basically the homelab lesson for iteration {i} stuck."
            )
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
                (
                    uuid7(),
                    auth.subject_id,
                    register,
                    content,
                    len(content.split()),
                    f"test-exemplar-{register}-{i:04d}",
                ),
            )


@pytest.fixture
def auth(ensure_db):
    apply_migrations()
    settings = get_settings()
    return resolve_auth(f"Bearer {settings.bootstrap_token.get_secret_value()}")


def test_quality_score_prefers_slop_clean(auth):
    good = quality_score(
        "I shipped the fix and moved on. The stack is stable enough for now. "
        "No need for a pep talk — just noting what changed in the homelab.",
        "casual_chat",
        register_length_mean=80.0,
    )
    bad = quality_score(
        "Great question! Here is a breakdown of the homelab stack and how it works. "
        "Does this match your experience? Let me know if you'd like more detail.",
        "casual_chat",
        register_length_mean=80.0,
    )
    assert good > bad


def test_select_exemplars_reaches_target_with_diverse_corpus():
    rows: list[_CandidateRow] = []
    for i in range(130):
        register = ("journal", "technical", "casual_chat")[i % 3]
        content = _diverse_exemplar_prose(register, i)
        wc = len(content.split())
        assert wc >= MIN_RESPONSE_WORDS, (register, i, wc)
        rows.append(
            _CandidateRow(
                id=uuid7(),
                register=register,
                content=content,
                source_kind="notion",
                source_ref={},
                mode_hint=None,
                word_count=wc,
                text_hash=f"unit-{register}-{i:04d}",
            )
        )
    _selected, stats = _select_exemplars(rows, {})
    assert stats.scored >= 100
    assert stats.selected >= 100


def test_sync_anti_patterns_and_build_exemplars(auth):
    n = sync_anti_patterns_from_slop_baseline(auth)
    assert n >= len(all_anti_pattern_seeds())

    report = run_build_voice_exemplars(auth, dry_run=False, embed=False)
    assert report["eligible_rows"] >= 1
    assert report["anti_patterns_written"] >= n

    with connection() as conn:
        anti = fetch_one(
            conn,
            "SELECT COUNT(*) AS n FROM anti_patterns WHERE subject_id = %s",
            (auth.subject_id,),
        )
        assert int(anti["n"]) >= n
        if report["selected"] > 0:
            sample = fetch_one(
                conn,
                """
                SELECT context, quality_score
                FROM voice_exemplars
                WHERE subject_id = %s AND status = 'candidate'
                ORDER BY quality_score DESC NULLS LAST
                LIMIT 1
                """,
                (auth.subject_id,),
            )
            ctx = sample["context"]
            if isinstance(ctx, str):
                ctx = json.loads(ctx)
            assert ctx.get("context_synthetic") is True
            assert sample["quality_score"] is not None


def test_seed_rows_insert_for_isolated_scoring(auth):
    _seed_train_rows(auth, register="notes", count=3)
    with connection() as conn:
        row = fetch_one(
            conn,
            "SELECT COUNT(*) AS n FROM authored_texts WHERE subject_id = %s AND text_hash LIKE 'test-exemplar-notes-%%'",
            (auth.subject_id,),
        )
        assert int(row["n"]) >= 3
