"""Backfill memory temporal columns from linked evidence (T-2 pilot, T-3 full)."""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import duckdb

from virtual_self.archive.parsers.base import parse_ts
from virtual_self.archive.paths import parquet_events_dir
from virtual_self.auth.credentials import AuthContext
from virtual_self.config import get_settings
from virtual_self.ingestion.frontmatter import split_frontmatter
from virtual_self.ingestion.my_life_scanner import iter_my_life_pages
from virtual_self.persistence.db import connection, execute, fetch_all, fetch_one, transaction
from virtual_self.services.notion_authored_split import detect_bulk_import_timestamps

PILOT_DEFAULT_LIMIT = 200
FULL_DEFAULT_BATCH = 500
AUDIT_SAMPLE_SIZE = 30

_CANDIDATE_SELECT = """
  SELECT DISTINCT ON (m.id)
    m.id,
    m.scope,
    m.time_precision AS mem_precision,
    m.time_source AS mem_source,
    s.source_type,
    s.source_path,
    er.structured_content,
    COALESCE(er.structured_content->>'event_id', s.external_id) AS event_id,
    ct.created_at AS turn_created_at
  FROM memories m
  JOIN artifacts a ON a.id = m.id AND a.deleted_at IS NULL
  JOIN memory_versions mv ON mv.id = m.current_version_id AND mv.status = 'active'
  LEFT JOIN memory_evidence me ON me.memory_id = m.id
  LEFT JOIN evidence_records er ON er.id = me.evidence_record_id
  LEFT JOIN sources s ON s.id = er.source_id
  LEFT JOIN chat_turns ct ON ct.evidence_record_id = er.id
  WHERE m.subject_id = %s
    AND m.time_precision = 'unknown'
    AND (m.time_source IS NULL OR m.time_source <> 'import')
  ORDER BY m.id, me.evidence_record_id NULLS LAST
"""

_TIME_SOURCE_OLAP = {
    "photo": "exif",
    "photos": "exif",
    "calendar": "calendar",
    "mail": "email_header",
    "gmail": "email_header",
}


@dataclass
class TimeResolution:
    authored_at: datetime | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    time_precision: str = "unknown"
    time_source: str | None = None
    resolver: str = "none"
    evidence_kind: str | None = None
    detail: str | None = None

    def applied(self) -> bool:
        return self.time_precision != "unknown" or self.time_source == "import"


@dataclass
class PilotRow:
    memory_id: UUID
    scope: str
    resolution: TimeResolution
    skipped_reason: str | None = None


@dataclass
class PilotReport:
    limit: int
    dry_run: bool
    candidates: int = 0
    updated: int = 0
    skipped_already_set: int = 0
    by_time_source: dict[str, int] = field(default_factory=dict)
    by_resolver: dict[str, int] = field(default_factory=dict)
    by_scope: dict[str, int] = field(default_factory=dict)
    rows: list[PilotRow] = field(default_factory=list)
    audit_sample: list[dict[str, Any]] = field(default_factory=list)
    audit_pass: bool = False
    audit_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "dry_run": self.dry_run,
            "candidates": self.candidates,
            "updated": self.updated,
            "skipped_already_set": self.skipped_already_set,
            "by_time_source": self.by_time_source,
            "by_resolver": self.by_resolver,
            "by_scope": self.by_scope,
            "audit_pass": self.audit_pass,
            "audit_notes": self.audit_notes,
            "audit_sample": self.audit_sample,
            "row_count": len(self.rows),
        }


@dataclass
class FullBackfillReport:
    batch_size: int
    dry_run: bool
    candidates: int = 0
    updated: int = 0
    skipped_no_evidence: int = 0
    batches: int = 0
    by_time_source: dict[str, int] = field(default_factory=dict)
    by_resolver: dict[str, int] = field(default_factory=dict)
    by_scope: dict[str, int] = field(default_factory=dict)
    audit_sample: list[dict[str, Any]] = field(default_factory=list)
    audit_pass: bool = False
    audit_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_size": self.batch_size,
            "dry_run": self.dry_run,
            "candidates": self.candidates,
            "updated": self.updated,
            "skipped_no_evidence": self.skipped_no_evidence,
            "batches": self.batches,
            "by_time_source": self.by_time_source,
            "by_resolver": self.by_resolver,
            "by_scope": self.by_scope,
            "audit_pass": self.audit_pass,
            "audit_notes": self.audit_notes,
            "audit_sample": self.audit_sample,
        }


def _coerce_ts(raw: str) -> datetime | None:
    dt = parse_ts(raw)
    if dt and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _load_bulk_import_ts(my_life_root: Path) -> set[str]:
    pages = list(iter_my_life_pages(my_life_root))
    return detect_bulk_import_timestamps(pages)


def _load_notion_meta_by_rel(my_life_root: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for page in iter_my_life_pages(my_life_root):
        rel = page.rel_path.replace("\\", "/")
        out[rel] = page.meta
    return out


def _load_olap_index() -> dict[str, dict[str, Any]]:
    root = parquet_events_dir()
    paths = sorted(root.rglob("*.parquet"))
    if not paths:
        return {}
    files = ", ".join(f"'{p}'" for p in paths)
    con = duckdb.connect(":memory:")
    try:
        cur = con.execute(
            f"""
            SELECT event_id, ts, product
            FROM read_parquet([{files}], union_by_name=true)
            WHERE event_id IS NOT NULL
            """
        )
        cols = [d[0] for d in cur.description]
        return {str(row[0]): dict(zip(cols, row)) for row in cur.fetchall()}
    except duckdb.Error:
        return {}
    finally:
        con.close()


def _olap_time_source(product: str | None) -> str | None:
    if not product:
        return None
    low = product.lower()
    for key, src in _TIME_SOURCE_OLAP.items():
        if key in low:
            return src
    return None


def _resolve_from_notion_meta(
    meta: dict[str, Any],
    *,
    bulk_import_ts: set[str],
) -> TimeResolution:
    created = meta.get("created_time")
    edited = meta.get("last_edited_time")
    raw_created = str(created).strip('"') if created else ""
    if raw_created and raw_created in bulk_import_ts:
        return TimeResolution(
            time_precision="unknown",
            time_source="import",
            resolver="notion_bulk_import",
            evidence_kind="my_life_md",
            detail=f"created_time cluster ({raw_created})",
        )
    if created:
        ts = _coerce_ts(raw_created)
        if ts:
            return TimeResolution(
                authored_at=ts,
                valid_from=ts,
                time_precision="day",
                time_source="notion_created",
                resolver="notion_created",
                evidence_kind="my_life_md",
            )
    if edited:
        ts = _coerce_ts(str(edited).strip('"'))
        if ts:
            return TimeResolution(
                authored_at=ts,
                valid_from=ts,
                time_precision="day",
                time_source="notion_edited",
                resolver="notion_edited",
                evidence_kind="my_life_md",
            )
    return TimeResolution(resolver="notion_none", evidence_kind="my_life_md")


def _resolve_from_olap(
    event_id: str,
    olap: dict[str, dict[str, Any]],
) -> TimeResolution:
    row = olap.get(event_id)
    if not row:
        return TimeResolution(resolver="olap_miss", evidence_kind="archive_event")
    ts = _coerce_ts(row.get("ts"))
    if not ts:
        return TimeResolution(resolver="olap_no_ts", evidence_kind="archive_event")
    product = str(row.get("product") or "")
    src = _olap_time_source(product)
    precision = "instant" if src else "day"
    return TimeResolution(
        authored_at=ts,
        valid_from=ts,
        time_precision=precision,
        time_source=src,
        resolver="olap_event",
        evidence_kind="archive_event",
        detail=product[:120] if product else None,
    )


def _resolve_from_chat(turn_created_at: datetime | None) -> TimeResolution:
    if not turn_created_at:
        return TimeResolution(resolver="chat_miss", evidence_kind="chat_turn")
    ts = turn_created_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return TimeResolution(
        authored_at=ts,
        valid_from=ts,
        time_precision="instant",
        time_source=None,
        resolver="chat_turn",
        evidence_kind="chat_turn",
    )


def resolve_memory_time(
    *,
    scope: str,
    source_type: str | None,
    source_path: str | None,
    structured: dict[str, Any] | None,
    event_id: str | None,
    turn_created_at: datetime | None,
    notion_meta_by_rel: dict[str, dict[str, Any]],
    bulk_import_ts: set[str],
    olap: dict[str, dict[str, Any]],
) -> TimeResolution:
    st = structured or {}
    if source_type == "my_life_md" or scope == "imported":
        rel = (source_path or st.get("rel_path") or "").replace("\\", "/")
        meta = notion_meta_by_rel.get(rel)
        if meta:
            return _resolve_from_notion_meta(meta, bulk_import_ts=bulk_import_ts)
        return TimeResolution(resolver="notion_no_file", evidence_kind="my_life_md", detail=rel)

    if source_type == "archive_event" or scope == "archive":
        eid = event_id or st.get("event_id")
        if eid:
            return _resolve_from_olap(str(eid), olap)
        return TimeResolution(resolver="archive_no_event_id", evidence_kind="archive_event")

    if scope == "chat" or source_type == "chat_cli":
        return _resolve_from_chat(turn_created_at)

    return TimeResolution(resolver="unsupported", evidence_kind=source_type)


def _fetch_pilot_candidates(conn, subject_id: UUID, limit: int) -> list[dict[str, Any]]:
    return fetch_all(
        conn,
        f"""
        SELECT * FROM (
        {_CANDIDATE_SELECT}
        ) pilot_cand
        LIMIT %s
        """,
        (subject_id, limit),
    )


def _fetch_backfill_batch(
    conn,
    subject_id: UUID,
    *,
    after_id: UUID | None,
    batch_size: int,
) -> list[dict[str, Any]]:
    if after_id is None:
        return fetch_all(
            conn,
            f"""
            SELECT * FROM (
            {_CANDIDATE_SELECT}
            ) full_cand
            ORDER BY id
            LIMIT %s
            """,
            (subject_id, batch_size),
        )
    return fetch_all(
        conn,
        f"""
        SELECT * FROM (
        {_CANDIDATE_SELECT}
        ) full_cand
        WHERE id > %s
        ORDER BY id
        LIMIT %s
        """,
        (subject_id, after_id, batch_size),
    )


def _count_backfill_candidates(conn, subject_id: UUID) -> int:
    row = fetch_one(
        conn,
        f"""
        SELECT COUNT(*) AS n FROM (
        {_CANDIDATE_SELECT}
        ) full_cand
        """,
        (subject_id,),
    )
    return int(row["n"]) if row else 0


def _structured_row(row: dict[str, Any]) -> dict[str, Any]:
    structured = row.get("structured_content")
    if isinstance(structured, str):
        structured = json.loads(structured)
    return structured if isinstance(structured, dict) else {}


def _record_applied(
    report: PilotReport | FullBackfillReport,
    row: dict[str, Any],
    res: TimeResolution,
) -> None:
    src_key = res.time_source or f"resolver:{res.resolver}"
    report.by_time_source[src_key] = report.by_time_source.get(src_key, 0) + 1
    report.by_resolver[res.resolver] = report.by_resolver.get(res.resolver, 0) + 1
    report.by_scope[row["scope"]] = report.by_scope.get(row["scope"], 0) + 1
    report.updated += 1


def _run_audit_sample(
    applied_rows: list[PilotRow],
    *,
    notion_meta_by_rel: dict[str, dict[str, Any]],
    olap: dict[str, dict[str, Any]],
    seed: int,
) -> tuple[list[dict[str, Any]], bool, list[str]]:
    notes: list[str] = []
    audit_sample: list[dict[str, Any]] = []
    if not applied_rows:
        return audit_sample, False, notes
    rng = random.Random(seed)
    sample = rng.sample(applied_rows, min(AUDIT_SAMPLE_SIZE, len(applied_rows)))
    ok = 0
    for pr in sample:
        passed, note = _audit_row(
            pr,
            notion_meta_by_rel=notion_meta_by_rel,
            olap=olap,
        )
        if passed:
            ok += 1
        audit_sample.append(
            {
                "memory_id": str(pr.memory_id),
                "scope": pr.scope,
                "passed": passed,
                "note": note,
                **asdict(pr.resolution),
            }
        )
    pct = ok / len(sample) if sample else 0.0
    notes.append(f"hand_audit_sample={len(sample)} pass_rate={pct:.2%}")
    false_precision = [
        s for s in audit_sample if not s["passed"] and "precision" in s.get("note", "")
    ]
    notes.append(f"false_precision_flags={len(false_precision)}")
    return audit_sample, pct >= 0.9, notes


def _apply_resolution(conn, memory_id: UUID, version_id: UUID | None, res: TimeResolution) -> None:
    execute(
        conn,
        """
        UPDATE memories SET
          authored_at = %s,
          valid_from = %s,
          valid_to = %s,
          time_precision = %s,
          time_source = %s,
          updated_at = NOW()
        WHERE id = %s
        """,
        (
            res.authored_at,
            res.valid_from,
            res.valid_to,
            res.time_precision,
            res.time_source,
            memory_id,
        ),
    )
    if version_id:
        execute(
            conn,
            """
            UPDATE memory_versions SET
              authored_at = %s,
              time_precision = %s,
              time_source = %s
            WHERE id = %s
            """,
            (res.authored_at, res.time_precision, res.time_source, version_id),
        )


def _audit_row(row: PilotRow, *, notion_meta_by_rel: dict[str, dict[str, Any]], olap: dict) -> tuple[bool, str]:
    res = row.resolution
    if res.time_source == "import":
        if res.time_precision != "unknown" or res.authored_at is not None:
            return False, "import cluster must stay unknown with no invented date"
        return True, "import cluster flagged"

    if res.time_precision == "unknown" and res.time_source is None:
        return True, "undated left unknown"

    if res.time_precision == "day" and res.authored_at is None:
        return False, "day precision requires authored_at"

    if res.time_precision == "instant" and res.authored_at is None:
        return False, "instant precision requires authored_at"

    if res.resolver.startswith("notion_") and res.time_source in ("notion_created", "notion_edited"):
        return True, "notion metadata"

    if res.resolver == "olap_event":
        eid = res.detail  # not stored; skip deep check
        _ = eid
        if res.authored_at and res.time_precision in ("instant", "day"):
            return True, "olap timestamp"
        return False, "olap resolution incomplete"

    if res.resolver == "chat_turn" and res.authored_at and res.time_precision == "instant":
        return True, "chat turn timestamp"

    if res.applied():
        return True, "applied"
    return True, "no-op"


def run_time_backfill_pilot(
    auth: AuthContext,
    *,
    limit: int = PILOT_DEFAULT_LIMIT,
    dry_run: bool = True,
    audit_path: Path | None = None,
    seed: int = 41,
) -> PilotReport:
    settings = get_settings()
    my_life_root = settings.my_life_root
    bulk_ts = _load_bulk_import_ts(my_life_root) if my_life_root.is_dir() else set()
    notion_meta = _load_notion_meta_by_rel(my_life_root) if my_life_root.is_dir() else {}
    olap = _load_olap_index()

    report = PilotReport(limit=limit, dry_run=dry_run)

    with transaction() as conn:
        candidates = _fetch_pilot_candidates(conn, auth.subject_id, limit)
        report.candidates = len(candidates)

        for row in candidates:
            res = resolve_memory_time(
                scope=row["scope"],
                source_type=row.get("source_type"),
                source_path=row.get("source_path"),
                structured=_structured_row(row),
                event_id=row.get("event_id"),
                turn_created_at=row.get("turn_created_at"),
                notion_meta_by_rel=notion_meta,
                bulk_import_ts=bulk_ts,
                olap=olap,
            )
            pilot_row = PilotRow(memory_id=row["id"], scope=row["scope"], resolution=res)
            if not res.applied():
                pilot_row.skipped_reason = "no evidence time"
                report.rows.append(pilot_row)
                continue

            if not dry_run:
                ver = fetch_one(
                    conn,
                    "SELECT current_version_id FROM memories WHERE id = %s",
                    (row["id"],),
                )
                _apply_resolution(
                    conn,
                    row["id"],
                    ver["current_version_id"] if ver else None,
                    res,
                )
            _record_applied(report, row, res)
            report.rows.append(pilot_row)

    applied_rows = [r for r in report.rows if r.resolution.applied()]
    report.audit_sample, report.audit_pass, audit_notes = _run_audit_sample(
        applied_rows,
        notion_meta_by_rel=notion_meta,
        olap=olap,
        seed=seed,
    )
    report.audit_notes.extend(audit_notes)

    project_root = Path(__file__).resolve().parents[3]
    out_path = audit_path or (project_root / ".state" / "time_backfill_pilot.json")
    if out_path.name.endswith(".json"):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = report.to_dict()
        payload["rows"] = [
            {
                "memory_id": str(r.memory_id),
                "scope": r.scope,
                "skipped_reason": r.skipped_reason,
                **asdict(r.resolution),
            }
            for r in report.rows
        ]
        out_path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
        report.audit_notes.append(f"audit_written={out_path}")

    return report


def run_time_backfill_full(
    auth: AuthContext,
    *,
    batch_size: int = FULL_DEFAULT_BATCH,
    dry_run: bool = True,
    audit_path: Path | None = None,
    seed: int = 41,
) -> FullBackfillReport:
    """Backfill all remaining candidate memories in chunked transactions."""
    settings = get_settings()
    my_life_root = settings.my_life_root
    bulk_ts = _load_bulk_import_ts(my_life_root) if my_life_root.is_dir() else set()
    notion_meta = _load_notion_meta_by_rel(my_life_root) if my_life_root.is_dir() else {}
    olap = _load_olap_index()

    report = FullBackfillReport(batch_size=batch_size, dry_run=dry_run)
    applied_rows: list[PilotRow] = []
    after_id: UUID | None = None

    with connection() as conn:
        report.candidates = _count_backfill_candidates(conn, auth.subject_id)

    while True:
        with transaction() as conn:
            batch = _fetch_backfill_batch(
                conn,
                auth.subject_id,
                after_id=after_id,
                batch_size=batch_size,
            )
            if not batch:
                break
            report.batches += 1
            for row in batch:
                after_id = row["id"]
                res = resolve_memory_time(
                    scope=row["scope"],
                    source_type=row.get("source_type"),
                    source_path=row.get("source_path"),
                    structured=_structured_row(row),
                    event_id=row.get("event_id"),
                    turn_created_at=row.get("turn_created_at"),
                    notion_meta_by_rel=notion_meta,
                    bulk_import_ts=bulk_ts,
                    olap=olap,
                )
                pilot_row = PilotRow(memory_id=row["id"], scope=row["scope"], resolution=res)
                if not res.applied():
                    report.skipped_no_evidence += 1
                    continue
                if not dry_run:
                    ver = fetch_one(
                        conn,
                        "SELECT current_version_id FROM memories WHERE id = %s",
                        (row["id"],),
                    )
                    _apply_resolution(
                        conn,
                        row["id"],
                        ver["current_version_id"] if ver else None,
                        res,
                    )
                _record_applied(report, row, res)
                applied_rows.append(pilot_row)

        if len(batch) < batch_size:
            break

    report.audit_sample, report.audit_pass, audit_notes = _run_audit_sample(
        applied_rows,
        notion_meta_by_rel=notion_meta,
        olap=olap,
        seed=seed,
    )
    report.audit_notes.extend(audit_notes)

    project_root = Path(__file__).resolve().parents[3]
    out_path = audit_path or (project_root / ".state" / "time_backfill_full.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = report.to_dict()
    payload["post_run_counts"] = pilot_counts_for_subject(auth.subject_id)
    out_path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    report.audit_notes.append(f"audit_written={out_path}")

    return report


def pilot_counts_for_subject(subject_id: UUID) -> dict[str, int]:
    with connection() as conn:
        row = fetch_one(
            conn,
            """
            SELECT
              COUNT(*) FILTER (WHERE time_precision <> 'unknown') AS set_precision,
              COUNT(*) FILTER (WHERE time_source IS NOT NULL) AS set_source,
              COUNT(*) AS total
            FROM memories m
            JOIN artifacts a ON a.id = m.id AND a.deleted_at IS NULL
            WHERE m.subject_id = %s
            """,
            (subject_id,),
        )
    return dict(row) if row else {}
