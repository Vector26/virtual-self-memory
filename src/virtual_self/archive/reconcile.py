from __future__ import annotations

from typing import Any
from uuid import UUID

from virtual_self.archive import ledger
from virtual_self.persistence.db import execute


def phantom_reason(row: dict[str, Any]) -> str | None:
    """Return a reconcile error message if this ledger row is a phantom, else None."""
    name = str(row.get("file_name") or "")
    if name == "takeout-001.zip":
        return "reconcile: phantom takeout-001.zip"
    if name.startswith("mini-"):
        return "reconcile: phantom or test row"
    if name.startswith("takeout-remote"):
        if ledger.local_zip_path(row):
            return None
        return "reconcile: phantom or test row"
    file_path = row.get("file_path")
    if file_path is None or str(file_path).strip() == "":
        if ledger.local_zip_path(row) is None:
            return "reconcile: file_path null and zip missing"
    return None


def is_phantom_row(row: dict[str, Any]) -> bool:
    return phantom_reason(row) is not None


def list_phantom_rows(conn) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in ledger.list_files(conn):
        if row["state"] in ("done", "failed"):
            continue
        reason = phantom_reason(row)
        if reason:
            out.append({**row, "phantom_reason": reason})
    return out


def _delete_archive_file(conn, file_id: UUID) -> None:
    execute(conn, "DELETE FROM archive_runs WHERE archive_file_id = %s", (file_id,))
    execute(conn, "DELETE FROM archive_files WHERE id = %s", (file_id,))


def reconcile_phantoms(
    conn,
    *,
    dry_run: bool = False,
    delete: bool = False,
) -> dict[str, Any]:
    """Mark phantom rows failed; optionally delete them when not dry-run."""
    stats: dict[str, Any] = {
        "dry_run": dry_run,
        "phantoms_found": 0,
        "phantom_marked_failed": 0,
        "phantom_deleted": 0,
        "phantoms": [],
    }
    for row in list_phantom_rows(conn):
        stats["phantoms_found"] += 1
        reason = row["phantom_reason"]
        entry = {
            "id": str(row["id"]),
            "file_name": row.get("file_name"),
            "state": row.get("state"),
            "reason": reason,
        }
        stats["phantoms"].append(entry)
        if dry_run:
            continue
        if delete:
            _delete_archive_file(conn, row["id"])
            stats["phantom_deleted"] += 1
        else:
            ledger.set_state(conn, row["id"], "failed", error=reason)
            stats["phantom_marked_failed"] += 1
    return stats


def reconcile_inflight(conn, *, dry_run: bool = False) -> dict[str, int]:
    """Reset stuck pipeline rows when zip exists; fail when zip is gone."""
    stats = {
        "inflight_unchanged": 0,
        "inflight_marked_failed": 0,
    }
    for row in ledger.list_files(conn):
        if row["state"] not in ("unpacked", "parsed", "analyzed", "promoted"):
            continue
        if ledger.local_zip_path(row):
            stats["inflight_unchanged"] += 1
        else:
            if not dry_run:
                ledger.set_state(conn, row["id"], "failed", error="reconcile: inflight without zip")
            stats["inflight_marked_failed"] += 1
    return stats
