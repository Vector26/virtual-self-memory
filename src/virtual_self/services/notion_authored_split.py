"""Read-only Notion export walk → authored_texts (persona P1 splitter)."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from uuid6 import uuid7

from virtual_self.auth.credentials import AuthContext
from virtual_self.eval.heldout_snapshot import infer_register, split_key_notion_source, stable_split_bucket_from_key
from virtual_self.eval.notion_self_text import split_body_authorship, word_count
from virtual_self.ingestion.my_life_scanner import ScannedPage, iter_my_life_pages
from virtual_self.persistence.db import connection, transaction
from virtual_self.policy.my_life_import import ImportDisposition
from virtual_self.policy.sensitivity import classify_source_path

_IMPORT_TS_MIN_PAGES = 5
_SKIP_NAME_FRAGMENTS = ("index.md", "rows.csv", "schema.json")
_GPT_IN_TITLE = re.compile(r"\b(by gpt|gpt generated|chatgpt)\b", re.I)


@dataclass
class SplitStats:
    pages_total: int = 0
    pages_processed: int = 0
    pages_template: int = 0
    pages_skipped: int = 0
    pages_empty: int = 0
    rows_inserted: int = 0
    rows_skipped_duplicate: int = 0
    rows_by_author: dict[str, int] = field(default_factory=lambda: Counter())
    import_timestamp_values: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pages_total": self.pages_total,
            "pages_processed": self.pages_processed,
            "pages_template": self.pages_template,
            "pages_skipped": self.pages_skipped,
            "pages_empty": self.pages_empty,
            "rows_inserted": self.rows_inserted,
            "rows_skipped_duplicate": self.rows_skipped_duplicate,
            "rows_by_author": dict(self.rows_by_author),
            "import_timestamp_values": self.import_timestamp_values,
            "pages_reconciled": self.pages_processed
            + self.pages_template
            + self.pages_skipped
            + self.pages_empty,
        }


def _under_area(norm: str, area: str) -> bool:
    if norm == area or norm.startswith(area + "/"):
        return True
    return norm.startswith(area + " [") or norm.startswith(area + "[")


def _page_outcome(page: ScannedPage) -> str:
    rel = page.rel_path.replace("\\", "/")
    name = page.abs_path.name.lower()
    if any(s in name for s in _SKIP_NAME_FRAGMENTS):
        return "skipped"
    if _GPT_IN_TITLE.search(page.abs_path.stem):
        return "skipped"

    if _under_area(rel, "Custom Templates"):
        return "template"

    if page.decision.disposition is ImportDisposition.SKIP:
        return "skipped"

    if page.decision.disposition is ImportDisposition.METADATA_ONLY:
        if page.decision.reason in ("title_only_stub", "not_a_page"):
            return "template"
        # Thin journal/people pages may still carry user prose in body.
        if word_count(page.body) < 3:
            return "template"

    return "processed"


def detect_bulk_import_timestamps(pages: list[ScannedPage]) -> set[str]:
    counts: Counter[str] = Counter()
    for page in pages:
        raw = page.meta.get("created_time")
        if raw:
            counts[str(raw).strip('"')] += 1
    return {ts for ts, n in counts.items() if n >= _IMPORT_TS_MIN_PAGES}


def _parse_authored_at(meta: dict[str, Any]) -> tuple[datetime | None, str | None, str]:
    created = meta.get("created_time")
    edited = meta.get("last_edited_time")
    if created:
        return _coerce_ts(str(created).strip('"')), "notion_created", "day"
    if edited:
        return _coerce_ts(str(edited).strip('"')), "notion_edited", "day"
    return None, None, "unknown"


def _coerce_ts(raw: str) -> datetime | None:
    try:
        if raw.endswith("Z"):
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _author_confidence(author: str, *, structural_llm: bool) -> float:
    if author == "llm" and structural_llm:
        return 1.0
    if author == "self" and structural_llm:
        return 0.98
    if author == "quoted":
        return 0.9
    if author == "self":
        return 0.95
    return 0.85


def split_page(page: ScannedPage) -> list[tuple[str, str, dict[str, str]]]:
    return split_body_authorship(page.body)


def run_notion_authored_split(
    auth: AuthContext,
    root: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    root = root.resolve()
    pages = list(iter_my_life_pages(root))
    bulk_ts = detect_bulk_import_timestamps(pages)
    stats = SplitStats(pages_total=len(pages), import_timestamp_values=sorted(bulk_ts))

    pending_rows: list[tuple[Any, ...]] = []

    for page in pages:
        outcome = _page_outcome(page)
        if outcome == "template":
            stats.pages_template += 1
            continue
        if outcome == "skipped":
            stats.pages_skipped += 1
            continue

        segments = split_page(page)
        if not segments:
            stats.pages_empty += 1
            continue

        stats.pages_processed += 1
        structural_llm = any(a == "llm" for a, _, _ in segments)
        rel = page.rel_path.replace("\\", "/")
        register = infer_register(rel, "notion")
        sensitivity = classify_source_path(rel)
        authored_at, time_source, time_precision = _parse_authored_at(page.meta)
        created_raw = str(page.meta.get("created_time") or "").strip('"')
        import_suspect = created_raw in bulk_ts
        split = stable_split_bucket_from_key(split_key_notion_source(page.abs_path))

        for idx, (author, content, extra) in enumerate(segments):
            stats.rows_by_author[author] += 1
            if dry_run:
                continue

            text_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            source_ref = {
                "rel_path": rel,
                "notion_id": page.notion_id,
                "segment_index": idx,
                "author": author,
                **extra,
            }
            if import_suspect:
                source_ref["import_timestamp_suspect"] = True

            pending_rows.append(
                (
                    uuid7(),
                    auth.subject_id,
                    "notion",
                    json.dumps(source_ref),
                    author,
                    _author_confidence(author, structural_llm=structural_llm),
                    register,
                    content,
                    word_count(content),
                    text_hash,
                    sensitivity,
                    authored_at,
                    time_precision,
                    time_source,
                    split,
                )
            )

    inserted = 0
    dupes = 0
    if not dry_run and pending_rows:
        with transaction() as conn:
            for row in pending_rows:
                cur = conn.execute(
                    """
                    INSERT INTO authored_texts (
                      id, subject_id, source_kind, source_ref, author, author_confidence,
                      register, content, word_count, text_hash, sensitivity,
                      authored_at, time_precision, time_source, split
                    ) VALUES (
                      %s, %s, %s, %s::jsonb, %s, %s,
                      %s, %s, %s, %s, %s,
                      %s, %s, %s, %s
                    )
                    ON CONFLICT (subject_id, text_hash) DO NOTHING
                    """,
                    row,
                )
                if cur.rowcount:
                    inserted += 1
                else:
                    dupes += 1

    stats.rows_inserted = inserted
    stats.rows_skipped_duplicate = dupes

    out = stats.to_dict()
    out["root"] = str(root)
    out["dry_run"] = dry_run
    if stats.pages_total != out["pages_reconciled"]:
        out["reconcile_error"] = (
            f"pages_total {stats.pages_total} != reconciled {out['pages_reconciled']}"
        )
    return out


def sample_self_rows(subject_id: UUID, *, limit: int = 10) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT content, source_ref, word_count, register
            FROM authored_texts
            WHERE subject_id = %s AND source_kind = 'notion' AND author = 'self'
            ORDER BY RANDOM()
            LIMIT %s
            """,
            (subject_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]
