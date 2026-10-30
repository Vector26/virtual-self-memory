from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from virtual_self.persistence.db import execute, fetch_all, fetch_one, transaction
from uuid6 import uuid7

ARCHIVE_STATES = (
    "discovered",
    "stable",
    "downloaded",
    "unpacked",
    "parsed",
    "analyzed",
    "promoted",
    "done",
    "failed",
)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def upsert_discovered(
    conn,
    *,
    provider: str,
    file_name: str,
    size_bytes: int,
    file_path: str | None = None,
    subject_id: UUID | None = None,
) -> UUID:
    row = fetch_one(
        conn,
        """
        SELECT id FROM archive_files
        WHERE provider = %s AND file_name = %s
        LIMIT 1
        """,
        (provider, file_name),
    )
    if row:
        execute(
            conn,
            """
            UPDATE archive_files SET size_bytes = %s, file_path = %s, updated_at = NOW()
            WHERE id = %s
            """,
            (size_bytes, file_path, row["id"]),
        )
        return row["id"]
    fid = uuid7()
    execute(
        conn,
        """
        INSERT INTO archive_files (id, subject_id, provider, file_name, file_path, size_bytes, state)
        VALUES (%s, %s, %s, %s, %s, %s, 'discovered')
        """,
        (fid, subject_id, provider, file_name, file_path, size_bytes),
    )
    return fid


def get_by_sha256(conn, sha256: str) -> dict[str, Any] | None:
    return fetch_one(conn, "SELECT * FROM archive_files WHERE sha256 = %s", (sha256,))


def set_state(
    conn,
    file_id: UUID,
    state: str,
    *,
    error: str | None = None,
    meta_patch: dict[str, Any] | None = None,
) -> None:
    if meta_patch:
        execute(
            conn,
            """
            UPDATE archive_files SET state = %s, error_message = %s,
              meta = meta || %s::jsonb, updated_at = NOW()
            WHERE id = %s
            """,
            (state, error, json.dumps(meta_patch), file_id),
        )
    else:
        execute(
            conn,
            """
            UPDATE archive_files SET state = %s, error_message = %s, updated_at = NOW()
            WHERE id = %s
            """,
            (state, error, file_id),
        )


def mark_downloaded(conn, file_id: UUID, sha256: str, path: str) -> None:
    execute(
        conn,
        """
        UPDATE archive_files SET state = 'downloaded', sha256 = %s, file_path = %s, updated_at = NOW()
        WHERE id = %s
        """,
        (sha256, path, file_id),
    )


def list_files(conn, state: str | None = None) -> list[dict[str, Any]]:
    if state:
        return fetch_all(
            conn,
            "SELECT * FROM archive_files WHERE state = %s ORDER BY created_at",
            (state,),
        )
    return fetch_all(conn, "SELECT * FROM archive_files ORDER BY created_at")


def start_run(
    conn,
    run_type: str,
    *,
    subject_id: UUID | None = None,
    archive_file_id: UUID | None = None,
) -> UUID:
    rid = uuid7()
    execute(
        conn,
        """
        INSERT INTO archive_runs (id, run_type, subject_id, archive_file_id, state)
        VALUES (%s, %s, %s, %s, 'running')
        """,
        (rid, run_type, subject_id, archive_file_id),
    )
    return rid


def finish_run(conn, run_id: UUID, success: bool, stats: dict[str, Any]) -> None:
    execute(
        conn,
        """
        UPDATE archive_runs SET state = %s, stats = %s::jsonb, finished_at = NOW()
        WHERE id = %s
        """,
        ("completed" if success else "failed", json.dumps(stats), run_id),
    )


def record_llm_run(
    conn,
    *,
    archive_run_id: UUID,
    layer: str,
    prompt_version: str,
    input_ref: dict[str, Any] | None,
    output_json: dict[str, Any] | None,
    verdict: str | None = None,
) -> UUID:
    lid = uuid7()
    execute(
        conn,
        """
        INSERT INTO archive_llm_runs (
          id, archive_run_id, layer, prompt_version, input_ref, output_json, verdict
        ) VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
        """,
        (
            lid,
            archive_run_id,
            layer,
            prompt_version,
            json.dumps(input_ref) if input_ref else None,
            json.dumps(output_json) if output_json else None,
            verdict,
        ),
    )
    return lid


def local_zip_path(row: dict[str, Any]) -> Path | None:
    from virtual_self.archive import paths

    provider = row.get("provider") or "google"
    file_path = row.get("file_path")
    if file_path:
        p = Path(file_path)
        if p.is_file():
            return p
    inbox = {
        "google": paths.inbox_google(),
        "meta": paths.inbox_meta(),
        "manual": paths.inbox_manual(),
    }.get(provider, paths.inbox_manual())
    candidate = inbox / str(row.get("file_name") or "")
    return candidate if candidate.is_file() else None


def reconcile_ledger(
    conn,
    *,
    mark_phantoms: bool = True,
    reset_inflight: bool = True,
    dry_run: bool = False,
    delete_phantoms: bool = False,
) -> dict[str, Any]:
    """Fix phantom test rows and resume-safe reset for stuck pipeline states."""
    from virtual_self.archive import reconcile as reconcile_mod

    stats: dict[str, Any] = {
        "phantom_marked_failed": 0,
        "phantom_deleted": 0,
        "phantoms_found": 0,
        "inflight_reset_downloaded": 0,
        "inflight_marked_failed": 0,
        "dry_run": dry_run,
    }
    if mark_phantoms:
        phantom_stats = reconcile_mod.reconcile_phantoms(
            conn, dry_run=dry_run, delete=delete_phantoms
        )
        stats["phantom_marked_failed"] = phantom_stats["phantom_marked_failed"]
        stats["phantom_deleted"] = phantom_stats["phantom_deleted"]
        stats["phantoms_found"] = phantom_stats["phantoms_found"]
        stats["phantoms"] = phantom_stats.get("phantoms", [])
    if reset_inflight:
        inflight = reconcile_mod.reconcile_inflight(conn, dry_run=dry_run)
        stats.update(inflight)
    return stats


def status_summary(conn) -> dict[str, Any]:
    by_state = fetch_all(
        conn,
        "SELECT state, COUNT(*) AS n FROM archive_files GROUP BY state ORDER BY state",
    )
    products = fetch_all(
        conn,
        """
        SELECT stats->'events_by_product' AS ebp FROM archive_runs
        WHERE run_type = 'archive.parse' AND state = 'completed'
        ORDER BY finished_at DESC NULLS LAST LIMIT 5
        """,
    )
    merged_products: dict[str, int] = {}
    for row in products:
        ebp = row.get("ebp") or {}
        if isinstance(ebp, dict):
            for k, v in ebp.items():
                merged_products[k] = merged_products.get(k, 0) + int(v)
    return {
        "files_by_state": {r["state"]: r["n"] for r in by_state},
        "events_by_product": merged_products,
    }
