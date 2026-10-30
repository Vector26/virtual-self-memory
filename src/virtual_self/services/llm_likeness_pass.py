"""Score author=self rows with char n-gram LLM-likeness; build review queue."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from virtual_self.auth.credentials import AuthContext
from virtual_self.eval.llm_likeness import (
    CharNgramLlmClassifier,
    MIN_SCORE_WORDS,
    MIN_TRAIN_WORDS,
    seed_llm_training_texts,
)
from virtual_self.eval.notion_self_text import word_count
from virtual_self.persistence.db import connection, transaction

REVIEW_DOC = Path(__file__).resolve().parents[3] / "docs" / "LLM_PASTE_REVIEW.md"
EXCLUDED_REASON = "likely_llm_paste"
# Flag self rows scoring above this tail of the trusted-self training distribution.
SELF_SCORE_TAIL = 0.97


def _threshold_from_self_training(clf: CharNgramLlmClassifier, self_texts: list[str]) -> float:
    scores = [
        clf.score(text)
        for text in self_texts
        if word_count(text) >= MIN_SCORE_WORDS and clf.score(text) != float("-inf")
    ]
    if not scores:
        return 0.0
    scores.sort()
    idx = min(len(scores) - 1, max(0, int(len(scores) * SELF_SCORE_TAIL)))
    return scores[idx]


@dataclass
class LlmLikenessStats:
    self_rows_total: int = 0
    self_rows_scored: int = 0
    self_rows_skipped_short: int = 0
    train_self_docs: int = 0
    train_llm_docs: int = 0
    flagged: int = 0
    cleared: int = 0
    threshold: float = 0.0
    flagged_rows: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "self_rows_total": self.self_rows_total,
            "self_rows_scored": self.self_rows_scored,
            "self_rows_skipped_short": self.self_rows_skipped_short,
            "train_self_docs": self.train_self_docs,
            "train_llm_docs": self.train_llm_docs,
            "flagged": self.flagged,
            "cleared": self.cleared,
            "threshold": self.threshold,
            "flagged_preview_count": len(self.flagged_rows),
        }


def _load_training_corpora(conn, subject_id: UUID) -> tuple[list[str], list[str]]:
    self_rows = conn.execute(
        """
        SELECT content
        FROM authored_texts
        WHERE subject_id = %s
          AND author = 'self'
          AND split = 'train'
          AND excluded_reason IS DISTINCT FROM %s
          AND word_count >= %s
        """,
        (subject_id, EXCLUDED_REASON, MIN_TRAIN_WORDS),
    ).fetchall()
    llm_rows = conn.execute(
        """
        SELECT content
        FROM authored_texts
        WHERE subject_id = %s
          AND author = 'llm'
          AND word_count >= %s
        """,
        (subject_id, MIN_TRAIN_WORDS),
    ).fetchall()
    self_texts = [str(r["content"]) for r in self_rows if r.get("content")]
    llm_texts = [str(r["content"]) for r in llm_rows if r.get("content")]
    llm_texts.extend(seed_llm_training_texts())
    return self_texts, llm_texts


def _fetch_self_rows(conn, subject_id: UUID) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT id, content, register, word_count, excluded_reason, source_kind, source_ref
        FROM authored_texts
        WHERE subject_id = %s
          AND author = 'self'
        ORDER BY word_count DESC, id
        """,
        (subject_id,),
    ).fetchall()


def render_review_doc(stats: LlmLikenessStats, *, generated_at: datetime | None = None) -> str:
    ts = (generated_at or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "# LLM paste review queue",
        "",
        (
            f"Generated: `{ts}` — char n-gram classifier (CPU). "
            f"Threshold log-odds: **{stats.threshold:.4f}**."
        ),
        "",
        (
            f"- Self rows scanned: **{stats.self_rows_total}** "
            f"(scored {stats.self_rows_scored}, skipped short {stats.self_rows_skipped_short})"
        ),
        (
            f"- Flagged `excluded_reason={EXCLUDED_REASON}`: **{stats.flagged}** "
            f"(cleared prior flags: {stats.cleared})"
        ),
        "",
        "Review these segments; confirm paste vs false positive before voice training.",
        "",
        "| register | words | score | source | preview |",
        "| --- | ---: | ---: | --- | --- |",
    ]
    for row in stats.flagged_rows:
        preview = (row.get("preview") or "").replace("|", "\\|").replace("\n", " ")
        if len(preview) > 100:
            preview = preview[:97] + "..."
        lines.append(
            f"| {row.get('register', '')} | {row.get('word_count', 0)} | "
            f"{row.get('score', 0.0):.3f} | {row.get('source_kind', '')} | {preview} |"
        )
    if not stats.flagged_rows:
        lines.append("| — | — | — | — | *(none above threshold)* |")
    lines.append("")
    return "\n".join(lines)


def run_llm_likeness_pass(auth: AuthContext, *, dry_run: bool = True) -> dict[str, Any]:
    stats = LlmLikenessStats()
    subject_id = auth.subject_id

    with connection() as conn:
        self_texts, llm_texts = _load_training_corpora(conn, subject_id)
        stats.train_self_docs = len(self_texts)
        stats.train_llm_docs = len(llm_texts)

        clf = CharNgramLlmClassifier().fit(self_texts, llm_texts)
        clf.threshold = _threshold_from_self_training(clf, self_texts)

        rows = _fetch_self_rows(conn, subject_id)
        stats.self_rows_total = len(rows)

        updates: list[tuple[str | None, UUID]] = []
        for row in rows:
            content = str(row["content"] or "")
            wc = int(row["word_count"] or word_count(content))
            if wc < MIN_SCORE_WORDS:
                stats.self_rows_skipped_short += 1
                continue
            stats.self_rows_scored += 1
            score = clf.score(content)
            flagged = score >= clf.threshold
            prev = row.get("excluded_reason")

            if flagged:
                stats.flagged += 1
                preview = content.replace("\n", " ")[:160]
                stats.flagged_rows.append(
                    {
                        "id": str(row["id"]),
                        "register": row.get("register"),
                        "word_count": wc,
                        "score": score,
                        "source_kind": row.get("source_kind"),
                        "preview": preview,
                    }
                )
                if prev != EXCLUDED_REASON:
                    updates.append((EXCLUDED_REASON, row["id"]))
            elif prev == EXCLUDED_REASON:
                stats.cleared += 1
                updates.append((None, row["id"]))

        stats.threshold = clf.threshold

    if dry_run:
        doc = render_review_doc(stats)
        return {"dry_run": True, "stats": stats.to_dict(), "review_doc": doc}

    with transaction() as conn:
        for reason, row_id in updates:
            conn.execute(
                """
                UPDATE authored_texts
                SET excluded_reason = %s
                WHERE id = %s AND subject_id = %s AND author = 'self'
                """,
                (reason, row_id, subject_id),
            )

    doc = render_review_doc(stats)
    REVIEW_DOC.parent.mkdir(parents=True, exist_ok=True)
    REVIEW_DOC.write_text(doc, encoding="utf-8")
    return {
        "dry_run": False,
        "stats": stats.to_dict(),
        "review_doc_path": str(REVIEW_DOC),
        "review_doc": doc,
    }
