from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from virtual_self.archive import ledger, paths, unpack
from virtual_self.archive.dataset import export_datasets
from virtual_self.archive.flow import classify_and_parse_staging
from virtual_self.archive.llm.pipeline import (
    consolidate_actions,
    extract_candidates_for_product,
    run_l1_sample,
    verify_candidate,
    window_digest,
)
from virtual_self.archive.olap import (
    batch_has_events,
    ensure_duckdb_views,
    fetch_events_by_ids,
    load_event_rows_for_batch,
    product_counts,
    read_events_connection_for_batch,
    write_events_parquet,
)
from virtual_self.archive.policy import apply_policy
from virtual_self.archive.promote import (
    count_archive_memories,
    promote_parsed_events,
    promote_verified_candidates,
)
from virtual_self.auth.credentials import AuthContext
from virtual_self.config import get_settings
from virtual_self.domain.errors import ForbiddenError, VirtualSelfError
from virtual_self.observability.notify import NotifyCategory, NotifySeverity, notify_user
from virtual_self.persistence.db import fetch_one, transaction
from virtual_self.services.memories import search_memories

_MAX_CONSECUTIVE_HARD_FAILURES = 5
_RECOVERABLE_PROCESS_ERRORS = frozenset({"zip missing"})


def process_archive_file(
    auth: AuthContext,
    row: dict[str, Any],
    *,
    analyze: bool = True,
    promote: bool = True,
) -> dict[str, Any]:
    """Unpack → parse → analyze → promote for one ledger row."""
    fid = row["id"]
    provider = row["provider"]
    zip_path = Path(row["file_path"] or "")
    if not zip_path.is_file():
        inbox = {
            "google": paths.inbox_google(),
            "meta": paths.inbox_meta(),
            "manual": paths.inbox_manual(),
        }.get(provider, paths.inbox_manual())
        zip_path = inbox / row["file_name"]
    if not zip_path.is_file():
        with transaction() as conn:
            ledger.set_state(conn, fid, "failed", error="zip missing")
        return {"ok": False, "error": "zip missing"}

    stats: dict[str, Any] = {
        "files_seen": 0,
        "events_parsed": 0,
        "excluded": {},
        "candidates": 0,
        "verified": 0,
        "promoted": 0,
        "rejected": 0,
    }

    with transaction() as conn:
        run_id = ledger.start_run(conn, "archive.unpack", subject_id=auth.subject_id, archive_file_id=fid)

    ledger_state = str(row.get("state") or "downloaded")
    if ledger_state == "done":
        return {"ok": True, "skipped": "already_done"}

    staging = paths.staging_dir(str(fid))
    try:
        return _process_archive_file_body(
            auth,
            row,
            fid=fid,
            zip_path=zip_path,
            run_id=run_id,
            staging=staging,
            stats=stats,
            analyze=analyze,
            promote=promote,
        )
    except Exception as exc:
        with transaction() as conn:
            cur = fetch_one(conn, "SELECT state FROM archive_files WHERE id = %s", (fid,))
            if cur and cur["state"] not in ("done", "failed"):
                ledger.set_state(conn, fid, "failed", error=str(exc)[:2000])
            ledger.finish_run(conn, run_id, False, {**stats, "error": str(exc)})
        out: dict[str, Any] = {"ok": False, "error": str(exc)}
        if _is_auth_error(exc):
            out["fatal"] = "auth"
        return out


def _staging_has_content(staging: Path) -> bool:
    if not staging.is_dir():
        return False
    return any(staging.rglob("*"))


def _product_counts_from_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
    by_product: dict[str, int] = {}
    for ev in rows:
        product = str(ev.get("product") or "unknown")
        by_product[product] = by_product.get(product, 0) + 1
    return by_product


def _process_archive_file_body(
    auth: AuthContext,
    row: dict[str, Any],
    *,
    fid: Any,
    zip_path: Path,
    run_id: Any,
    staging: Path,
    stats: dict[str, Any],
    analyze: bool,
    promote: bool,
) -> dict[str, Any]:
    settings = get_settings()
    provider = row["provider"]
    ledger_state = str(row.get("state") or "downloaded")
    batch = str(fid)[:8]
    skipped: dict[str, int] = {}
    unknown_samples: list[Any] = []
    promote_blocked = False
    event_rows: list[dict[str, Any]] = []
    by_product: dict[str, int] = {}

    if ledger_state in ("parsed", "analyzed", "promoted"):
        event_rows = load_event_rows_for_batch(batch)
        stats["events_parsed"] = len(event_rows)
        by_product = _product_counts_from_rows(event_rows)
        stats["resumed_from"] = ledger_state
    elif ledger_state == "unpacked" and batch_has_events(batch):
        event_rows = load_event_rows_for_batch(batch)
        stats["events_parsed"] = len(event_rows)
        by_product = _product_counts_from_rows(event_rows)
    else:
        if ledger_state == "downloaded" or not _staging_has_content(staging):
            try:
                n_files, _ = unpack.safe_unpack(zip_path, staging)
                stats["files_seen"] = n_files
                with transaction() as conn:
                    ledger.set_state(conn, fid, "unpacked")
            except Exception as exc:
                with transaction() as conn:
                    ledger.set_state(conn, fid, "failed", error=str(exc))
                    ledger.finish_run(conn, run_id, False, stats)
                return {"ok": False, "error": str(exc)}

        staging_out = classify_and_parse_staging(
            staging,
            provider,
            subject_id=auth.subject_id,
            archive_file_id=fid,
        )
        all_events = staging_out.events
        unknown_samples = staging_out.unknown_samples
        stats["quarantined"] = staging_out.quarantined
        stats["supervisor"] = staging_out.supervisor_meta
        promote_blocked = staging_out.block_promote

        kept, skipped = apply_policy(all_events)
        stats["excluded"] = skipped
        if not kept and not batch_has_events(batch):
            with transaction() as conn:
                ledger.set_state(
                    conn,
                    fid,
                    "failed",
                    error="no events parsed from archive zip",
                )
                ledger.finish_run(conn, run_id, False, stats)
            return {"ok": False, "error": "no events parsed"}
        write_events_parquet(kept, batch)
        try:
            ensure_duckdb_views()
        except Exception:
            pass
        event_rows = [ev.to_row() for ev in kept]
        stats["events_parsed"] = len(event_rows)
        by_product = _product_counts_from_rows(event_rows)

        with transaction() as conn:
            parse_run = ledger.start_run(
                conn, "archive.parse", subject_id=auth.subject_id, archive_file_id=fid
            )
            ledger.finish_run(
                conn,
                parse_run,
                True,
                {
                    "events": len(event_rows),
                    "events_by_product": by_product,
                    "excluded": skipped,
                },
            )
            ledger.set_state(conn, fid, "parsed")

    if ledger_state in ("parsed", "analyzed", "promoted") and not event_rows and not batch_has_events(
        batch
    ):
        with transaction() as conn:
            ledger.set_state(conn, fid, "failed", error="no parquet batch for resumed state")
            ledger.finish_run(conn, run_id, False, stats)
        return {"ok": False, "error": "missing batch parquet"}

    staging_cleared = False
    if ledger_state == "downloaded" and stats.get("files_seen"):
        if settings.archive_delete_zip_after and zip_path.is_file():
            zip_path.unlink(missing_ok=True)
        if settings.archive_streaming or settings.archive_delete_zip_after:
            unpack.cleanup_staging(staging)
            staging_cleared = True

        notify_user(
            f"Archive zip {row['file_name']}: files {stats['files_seen']}, "
            f"events {stats['events_parsed']}, excluded {sum(skipped.values())}.",
            category=NotifyCategory.ARCHIVE_ZIP,
            severity=NotifySeverity.INFO,
        )

    verified_all: list[dict[str, Any]] = []
    run_analyze = analyze and ledger_state in ("downloaded", "unpacked", "parsed")
    if run_analyze and settings.archive_llm_enabled:
        analyze_run: Any = None
        analyze_ok = False
        with transaction() as conn:
            analyze_run = ledger.start_run(
                conn, "archive.analyze", subject_id=auth.subject_id, archive_file_id=fid
            )
        try:
            for sample in unknown_samples[:5]:
                run_l1_sample(sample, analyze_run)
            for product in sorted(by_product.keys()):
                con = read_events_connection_for_batch(batch)
                try:
                    rows = con.execute(
                        "SELECT * FROM events WHERE product = ? LIMIT 200",
                        [product],
                    ).fetchall()
                    cols = [d[0] for d in con.description]
                    evdicts = [dict(zip(cols, r)) for r in rows]
                except Exception:
                    evdicts = []
                digest, cites = window_digest(evdicts, analyze_run)
                cands = extract_candidates_for_product(product, digest, cites, analyze_run)
                stats["candidates"] += len(cands)
                for c in cands:
                    if c.get("verdict") != "unsupported":
                        stats["verified"] += 1
                    verified_all.append(c)
            analyze_ok = True
            with transaction() as conn:
                ledger.set_state(conn, fid, "analyzed")
        finally:
            with transaction() as conn:
                if analyze_run is not None:
                    ledger.finish_run(
                        conn,
                        analyze_run,
                        analyze_ok,
                        {
                            "candidates": stats["candidates"],
                            "verified": stats["verified"],
                        },
                    )
    elif analyze and ledger_state in ("downloaded", "unpacked", "parsed"):
        with transaction() as conn:
            ledger.set_state(conn, fid, "analyzed")

    llm_promote_blocked = promote_blocked
    if llm_promote_blocked:
        stats["llm_promote_skipped"] = "specialist_quarantine"

    run_promote = promote and ledger_state in ("downloaded", "unpacked", "parsed", "analyzed")
    if run_promote:
        stats["archive_memories_before"] = count_archive_memories(auth)
        estats = promote_parsed_events(auth, event_rows)
        stats.update(estats)

        if verified_all and not llm_promote_blocked:
            events_by_id: dict[str, dict[str, Any]] = {}
            for c in verified_all:
                for eid in c.get("evidence_event_ids") or []:
                    if str(eid) not in events_by_id:
                        for ev in fetch_events_by_ids([str(eid)]):
                            events_by_id[str(ev["event_id"])] = ev
            existing = [h.get("content", "") for h in search_memories(auth, "preferences habits", limit=15)]
            actions = consolidate_actions(verified_all, existing, None)
            pstats = promote_verified_candidates(auth, verified_all, events_by_id, actions)
            stats.update(pstats)

        stats["archive_memories_after"] = count_archive_memories(auth)
        with transaction() as conn:
            ledger.set_state(conn, fid, "promoted")

    export_datasets(verified_all, auth=auth)
    with transaction() as conn:
        ledger.set_state(conn, fid, "done")
        ledger.finish_run(conn, run_id, True, stats)

    if not staging_cleared:
        unpack.cleanup_staging(staging)
    return {"ok": True, **stats, "events_by_product": by_product}


def _is_auth_error(exc: BaseException) -> bool:
    if isinstance(exc, ForbiddenError):
        return True
    if isinstance(exc, VirtualSelfError) and exc.status_code in (401, 403):
        return True
    return False


def _next_work_row(conn) -> dict[str, Any] | None:
    for row in ledger.list_files(conn):
        if row["state"] == "failed":
            continue
        if row["state"] == "downloaded":
            return row
    for row in ledger.list_files(conn):
        if row["state"] in ("unpacked", "parsed", "analyzed", "promoted"):
            return row
    return None


def run_flow(
    auth: AuthContext,
    *,
    analyze: bool = True,
    promote: bool = True,
) -> dict[str, Any]:
    """Producer-consumer: one zip download → full pipeline → repeat until idle."""
    from virtual_self.archive.sync import sync_one_next_stable_zip

    summary: dict[str, Any] = {"cycles": [], "products": {}, "stopped_reason": None}
    max_cycles = 500
    consecutive_hard_failures = 0

    for _ in range(max_cycles):
        with transaction() as conn:
            ledger.reconcile_ledger(conn, mark_phantoms=True, reset_inflight=True)
            row = _next_work_row(conn)

        if row is None:
            sync_out = sync_one_next_stable_zip(auth.subject_id)
            action = sync_out.get("action")
            reason = sync_out.get("reason")
            if not sync_out.get("ok", True) and reason == "disk_guard":
                summary["stopped_reason"] = "disk_guard"
                break
            if action in ("none", "waiting_stable") and sync_out.get("downloaded", 0) == 0:
                summary["stopped_reason"] = reason or action
                break
            if not sync_out.get("ok", True):
                summary["stopped_reason"] = reason or "sync_failed"
                consecutive_hard_failures += 1
                if consecutive_hard_failures >= _MAX_CONSECUTIVE_HARD_FAILURES:
                    summary["stopped_reason"] = f"too_many_failures:{reason or 'sync_failed'}"
                    break
                continue
            with transaction() as conn:
                row = _next_work_row(conn)
            if row is None and action not in (
                "downloaded",
                "marked_downloaded",
                "already_downloaded",
                "download_failed",
            ):
                continue

        if row is None:
            continue

        try:
            out = process_archive_file(auth, row, analyze=analyze, promote=promote)
        except Exception as exc:
            if _is_auth_error(exc):
                summary["stopped_reason"] = "auth"
                break
            raise
        summary["cycles"].append({"file": row["file_name"], **out})
        if not out.get("ok"):
            if out.get("fatal") == "auth":
                summary["stopped_reason"] = "auth"
                break
            err = out.get("error", "process_failed")
            summary["stopped_reason"] = err
            if err in _RECOVERABLE_PROCESS_ERRORS:
                consecutive_hard_failures = 0
                continue
            consecutive_hard_failures += 1
            if consecutive_hard_failures >= _MAX_CONSECUTIVE_HARD_FAILURES:
                summary["stopped_reason"] = f"too_many_failures:{err}"
                break
            continue
        consecutive_hard_failures = 0

    summary["products"] = product_counts()
    totals = {
        "events": sum(summary["products"].values()),
        "zips": len(summary["cycles"]),
    }
    notify_user(
        f"Archive flow finished: {totals['zips']} zips, events by product: "
        f"{json.dumps(summary['products'])[:400]}.",
        category=NotifyCategory.ARCHIVE_SUMMARY,
    )
    return summary


def run_pipeline(auth: AuthContext, *, sync_first: bool = True) -> dict[str, Any]:
    from virtual_self.archive.sync import sync_drive_and_inbox, sync_one_next_stable_zip

    if sync_first:
        if get_settings().archive_streaming:
            sync_one_next_stable_zip(auth.subject_id)
        else:
            sync_drive_and_inbox(auth.subject_id)
    summary: dict[str, Any] = {"processed": [], "products": {}}
    with transaction() as conn:
        for row in ledger.list_files(conn):
            if row["state"] == "discovered":
                path = row.get("file_path")
                if path and Path(path).is_file():
                    digest = ledger.sha256_file(Path(path))
                    ledger.mark_downloaded(conn, row["id"], digest, path)
        row = _next_work_row(conn)
    if row is not None:
        out = process_archive_file(auth, row)
        summary["processed"].append({"file": row["file_name"], **out})
    summary["products"] = product_counts()
    totals = {
        "events": sum(summary["products"].values()),
        "zips": len(summary["processed"]),
    }
    notify_user(
        f"Archive run finished: {totals['zips']} zips, events by product: "
        f"{json.dumps(summary['products'])[:400]}.",
        category=NotifyCategory.ARCHIVE_SUMMARY,
    )
    return summary


def archive_report(auth: AuthContext, limit: int = 20) -> dict[str, Any]:
    with transaction() as conn:
        status = ledger.status_summary(conn)
    products = product_counts()
    promoted = search_memories(auth, "archive", limit=limit)
    return {
        "status": status,
        "product_counts": products,
        "archive_memory_count": count_archive_memories(auth),
        "top_promoted": promoted,
    }
