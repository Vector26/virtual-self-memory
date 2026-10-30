"""Second-pass register + sensitivity tagging over authored_texts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from virtual_self.auth.credentials import AuthContext
from virtual_self.policy.authored_tags import AuthoredTagResult, classify_authored_text
from virtual_self.persistence.db import connection, transaction


@dataclass
class RegisterSensitivityStats:
    rows_total: int = 0
    rows_updated: int = 0
    register_changes: int = 0
    sensitivity_changes: int = 0
    excluded_set: int = 0
    ambiguous: int = 0
    by_register_rows: dict[str, int] = field(default_factory=dict)
    by_register_words: dict[str, int] = field(default_factory=dict)
    self_by_register_rows: dict[str, int] = field(default_factory=dict)
    self_by_register_words: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows_total": self.rows_total,
            "rows_updated": self.rows_updated,
            "register_changes": self.register_changes,
            "sensitivity_changes": self.sensitivity_changes,
            "excluded_set": self.excluded_set,
            "ambiguous": self.ambiguous,
            "by_register_rows": dict(sorted(self.by_register_rows.items())),
            "by_register_words": dict(sorted(self.by_register_words.items())),
            "self_by_register_rows": dict(sorted(self.self_by_register_rows.items())),
            "self_by_register_words": dict(sorted(self.self_by_register_words.items())),
        }


def _needs_update(row: dict[str, Any], tag: AuthoredTagResult) -> bool:
    if row["register"] != tag.register:
        return True
    if int(row["sensitivity"]) != tag.sensitivity:
        return True
    prev_ex = row.get("excluded_reason")
    if tag.excluded_reason and prev_ex != tag.excluded_reason:
        return True
    return False


def _accumulate_register_stats(stats: RegisterSensitivityStats, row: dict[str, Any]) -> None:
    reg = row["register"]
    wc = int(row["word_count"] or 0)
    stats.by_register_rows[reg] = stats.by_register_rows.get(reg, 0) + 1
    stats.by_register_words[reg] = stats.by_register_words.get(reg, 0) + wc
    if row.get("author") == "self":
        stats.self_by_register_rows[reg] = stats.self_by_register_rows.get(reg, 0) + 1
        stats.self_by_register_words[reg] = stats.self_by_register_words.get(reg, 0) + wc


def run_authored_register_sensitivity_pass(
    auth: AuthContext,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    stats = RegisterSensitivityStats()
    pending_updates: list[tuple[Any, ...]] = []

    with connection() as conn:
        rows = conn.execute(
            """
            SELECT id, source_kind, source_ref, author, register, sensitivity,
                   excluded_reason, content, word_count
            FROM authored_texts
            WHERE subject_id = %s
            ORDER BY created_at
            """,
            (auth.subject_id,),
        ).fetchall()

    stats.rows_total = len(rows)
    tagged_rows: list[dict[str, Any]] = []

    for row in rows:
        tag = classify_authored_text(
            source_kind=row["source_kind"],
            source_ref=row["source_ref"],
            content=row["content"] or "",
            author=row["author"],
        )
        if tag.ambiguous:
            stats.ambiguous += 1

        updated = dict(row)
        if _needs_update(row, tag):
            updated["register"] = tag.register
            updated["sensitivity"] = tag.sensitivity
            if tag.excluded_reason:
                updated["excluded_reason"] = tag.excluded_reason
            pending_updates.append(
                (
                    tag.register,
                    tag.sensitivity,
                    tag.excluded_reason or row.get("excluded_reason"),
                    row["id"],
                    auth.subject_id,
                )
            )
            stats.rows_updated += 1
            if row["register"] != tag.register:
                stats.register_changes += 1
            if int(row["sensitivity"]) != tag.sensitivity:
                stats.sensitivity_changes += 1
            if tag.excluded_reason and row.get("excluded_reason") != tag.excluded_reason:
                stats.excluded_set += 1

        tagged_rows.append(updated)

    for updated in tagged_rows:
        _accumulate_register_stats(stats, updated)

    if not dry_run and pending_updates:
        with transaction() as conn:
            for reg, sens, excl, row_id, subject_id in pending_updates:
                conn.execute(
                    """
                    UPDATE authored_texts
                    SET register = %s,
                        sensitivity = %s,
                        excluded_reason = COALESCE(%s, excluded_reason)
                    WHERE id = %s AND subject_id = %s
                    """,
                    (reg, sens, excl, row_id, subject_id),
                )

    out = stats.to_dict()
    out["dry_run"] = dry_run
    out["pending_updates"] = len(pending_updates)
    return out
