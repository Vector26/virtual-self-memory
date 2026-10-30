from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from virtual_self.archive import disk_guard, ledger, paths
from virtual_self.config import get_settings
from virtual_self.observability.logging import get_logger
from virtual_self.observability.notify import NotifyCategory, NotifySeverity, notify_user
from virtual_self.persistence.db import fetch_one, transaction

log = get_logger(__name__)

_RCLONE_MAX_ATTEMPTS = 3
_RCLONE_BACKOFF_SECONDS = (1.0, 2.0)


def _rclone_backoff(attempt: int) -> None:
    if attempt + 1 >= _RCLONE_MAX_ATTEMPTS:
        return
    delay = _RCLONE_BACKOFF_SECONDS[min(attempt, len(_RCLONE_BACKOFF_SECONDS) - 1)]
    time.sleep(delay)


def _rclone_lsf(remote_path: str) -> tuple[list[dict[str, str]], str | None]:
    """List remote zip files. Returns (entries, error_message). error is set only on failure."""
    settings = get_settings()
    cfg = settings.archive_rclone_config
    if not cfg.is_file():
        msg = f"rclone config missing: {cfg}"
        log.warning("archive_rclone_missing", path=str(cfg))
        return [], msg
    cmd = [
        "rclone",
        "lsjson",
        f"{settings.archive_rclone_remote}:{remote_path}",
        "--config",
        str(cfg),
    ]
    last_err = "rclone lsf failed"
    for attempt in range(_RCLONE_MAX_ATTEMPTS):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
            last_err = str(exc)
            log.warning(
                "archive_rclone_lsf_failed",
                error=last_err,
                attempt=attempt + 1,
                max_attempts=_RCLONE_MAX_ATTEMPTS,
            )
            _rclone_backoff(attempt)
            continue
        try:
            data = json.loads(out.stdout or "[]")
        except json.JSONDecodeError as exc:
            last_err = f"invalid lsjson output: {exc}"
            log.warning("archive_rclone_lsf_bad_json", error=last_err, attempt=attempt + 1)
            _rclone_backoff(attempt)
            continue
        zips = [
            x for x in data if x.get("IsDir") is not True and str(x.get("Name", "")).endswith(".zip")
        ]
        return zips, None
    return [], last_err


def _file_stable(path: Path, poll_sec: float) -> bool:
    if not path.is_file():
        return False
    s1 = path.stat().st_size
    time.sleep(poll_sec)
    if not path.is_file():
        return False
    s2 = path.stat().st_size
    return s1 == s2 and s1 > 0


def _rclone_copy_one(remote_name: str, dest: Path) -> tuple[bool, str | None]:
    settings = get_settings()
    remote = f"{settings.archive_rclone_remote}:{settings.archive_takeout_path}/{remote_name}"
    cmd = [
        "rclone",
        "copyto",
        remote,
        str(dest),
        "--config",
        str(settings.archive_rclone_config),
    ]
    last_err = "rclone copy failed"
    for attempt in range(_RCLONE_MAX_ATTEMPTS):
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=7200, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
            last_err = str(exc)
            log.warning(
                "archive_rclone_copy_failed",
                file=remote_name,
                error=last_err,
                attempt=attempt + 1,
                max_attempts=_RCLONE_MAX_ATTEMPTS,
            )
            _rclone_backoff(attempt)
            continue
        if dest.is_file():
            return True, None
        last_err = f"copyto finished but dest missing: {dest}"
        log.warning("archive_rclone_copy_no_dest", file=remote_name, attempt=attempt + 1)
        _rclone_backoff(attempt)
    return False, last_err


def scan_inbox_local(provider: str, inbox: Path) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    if not inbox.is_dir():
        return found
    for p in sorted(inbox.glob("*.zip")):
        found.append((p.name, p.stat().st_size))
    return found


def _should_skip_row(row: dict[str, Any] | None, local: Path, dest_dir: Path) -> bool:
    if row and row["state"] in ("parsed", "analyzed", "promoted", "done"):
        return True
    if row and row["sha256"]:
        if local.is_file() and ledger.sha256_file(local) == row["sha256"]:
            return True
    return False


def sync_one_next_stable_zip(subject_id: UUID | None = None) -> dict[str, Any]:
    """Download at most one pending Takeout zip (discovered → downloaded)."""
    if not get_settings().archive_enabled:
        return {"ok": False, "skipped": True, "reason": "archive_disabled"}
    if not disk_guard.ensure_disk_or_notify("sync"):
        return {"ok": False, "reason": "disk_guard"}

    dest_dir = paths.inbox_google()
    poll = get_settings().archive_stability_poll_seconds

    with transaction() as conn:
        run_id = ledger.start_run(conn, "archive.sync", subject_id=subject_id)

    remote_zips, lsf_err = _rclone_lsf(get_settings().archive_takeout_path)
    if lsf_err:
        err_result: dict[str, Any] = {
            "ok": False,
            "action": "none",
            "reason": "rclone_lsf_failed",
            "errors": [lsf_err],
            "downloaded": 0,
            "stable": 0,
            "discovered": 0,
        }
        with transaction() as conn:
            ledger.finish_run(conn, run_id, False, err_result)
        notify_user(
            f"Archive sync: remote listing failed after {_RCLONE_MAX_ATTEMPTS} attempts: {lsf_err}",
            category=NotifyCategory.ARCHIVE_DOWNLOAD,
            severity=NotifySeverity.USER_ACTIONABLE,
        )
        return err_result
    if not remote_zips:
        empty_result = {"ok": True, "action": "none", "reason": "no_remote_zips"}
        with transaction() as conn:
            ledger.finish_run(conn, run_id, True, empty_result)
        return empty_result

    result: dict[str, Any] = {
        "ok": True,
        "action": "none",
        "downloaded": 0,
        "stable": 0,
        "discovered": 0,
        "file_name": None,
        "errors": [],
    }

    for item in remote_zips:
        name = item["Name"]
        size = int(item.get("Size") or 0)
        with transaction() as conn:
            fid = ledger.upsert_discovered(
                conn, provider="google", file_name=name, size_bytes=size, subject_id=subject_id
            )
            row = fetch_one(conn, "SELECT * FROM archive_files WHERE id = %s", (fid,))
        result["discovered"] += 1

        if row and row["state"] == "downloaded":
            local = dest_dir / name
            if local.is_file():
                continue
            with transaction() as conn:
                ledger.set_state(conn, fid, "discovered")
            row = {**row, "state": "discovered"}

        if row and row["state"] in ("unpacked", "parsed", "analyzed", "promoted"):
            continue

        if _should_skip_row(row, dest_dir / name, dest_dir):
            continue

        if row and row["state"] not in ("discovered", "stable", "failed", "downloaded"):
            continue

        local = dest_dir / name
        if local.is_file():
            if not _file_stable(local, poll):
                with transaction() as conn:
                    ledger.set_state(conn, fid, "stable")
                result["stable"] += 1
                result["action"] = "waiting_stable"
                result["file_name"] = name
                break
            if not disk_guard.ensure_disk_for_zip(f"sync {name}", size):
                result["ok"] = False
                result["reason"] = "disk_guard"
                break
            digest = ledger.sha256_file(local)
            with transaction() as conn:
                existing = ledger.get_by_sha256(conn, digest)
                if existing:
                    canon = existing.get("file_path") or str(local)
                    ledger.mark_downloaded(conn, fid, digest, canon)
                else:
                    ledger.mark_downloaded(conn, fid, digest, str(local))
            result["downloaded"] = 1
            result["stable"] += 1
            result["action"] = "marked_downloaded"
            result["file_name"] = name
            result["file_id"] = str(fid)
            break

        if not disk_guard.ensure_disk_for_zip(f"download {name}", size):
            result["ok"] = False
            result["reason"] = "disk_guard"
            break
        ok, copy_err = _rclone_copy_one(name, local)
        if not ok:
            msg = copy_err or "rclone copy failed"
            result["errors"].append(msg)
            result["ok"] = False
            with transaction() as conn:
                ledger.set_state(conn, fid, "failed", error=msg)
            result["action"] = "download_failed"
            result["file_name"] = name
            break
        if not _file_stable(local, poll):
            result["action"] = "waiting_stable"
            result["file_name"] = name
            break
        result["stable"] += 1
        digest = ledger.sha256_file(local)
        with transaction() as conn:
            ledger.mark_downloaded(conn, fid, digest, str(local))
        result["downloaded"] = 1
        result["action"] = "downloaded"
        result["file_name"] = name
        result["file_id"] = str(fid)
        notify_user(
            f"Archive sync: downloaded {name} ({size / (1024**2):.1f} MB). "
            f"Ledger state=downloaded.",
            category=NotifyCategory.ARCHIVE_DOWNLOAD,
            severity=NotifySeverity.INFO,
        )
        break

    with transaction() as conn:
        ledger.finish_run(conn, run_id, result.get("ok", True), result)
    return result


def sync_drive_and_inbox(subject_id: UUID | None = None) -> dict[str, Any]:
    """Pull stable Takeout zips and register inbox drops."""
    if not get_settings().archive_enabled:
        return {"skipped": True, "reason": "archive_disabled"}

    settings = get_settings()
    if settings.archive_streaming:
        stats = sync_one_next_stable_zip(subject_id=subject_id)
        for provider, inbox in (("meta", paths.inbox_meta()), ("manual", paths.inbox_manual())):
            for name, size in scan_inbox_local(provider, inbox):
                with transaction() as conn:
                    ledger.upsert_discovered(
                        conn,
                        provider=provider,
                        file_name=name,
                        size_bytes=size,
                        file_path=str(inbox / name),
                        subject_id=subject_id,
                    )
                stats["discovered"] = int(stats.get("discovered", 0)) + 1
        return stats

    if not disk_guard.ensure_disk_or_notify("sync"):
        return {"ok": False, "reason": "disk_guard"}

    stats: dict[str, Any] = {"downloaded": 0, "stable": 0, "discovered": 0, "errors": []}
    dest_dir = paths.inbox_google()
    poll = settings.archive_stability_poll_seconds

    with transaction() as conn:
        run_id = ledger.start_run(conn, "archive.sync", subject_id=subject_id)

    remote_zips, lsf_err = _rclone_lsf(settings.archive_takeout_path)
    if lsf_err:
        stats["ok"] = False
        stats["reason"] = "rclone_lsf_failed"
        stats["errors"] = [lsf_err]
        with transaction() as conn:
            ledger.finish_run(conn, run_id, False, stats)
        notify_user(
            f"Archive sync: remote listing failed after {_RCLONE_MAX_ATTEMPTS} attempts: {lsf_err}",
            category=NotifyCategory.ARCHIVE_DOWNLOAD,
            severity=NotifySeverity.USER_ACTIONABLE,
        )
        return stats

    run_ok = True
    for item in remote_zips:
        name = item["Name"]
        size = int(item.get("Size") or 0)
        with transaction() as conn:
            fid = ledger.upsert_discovered(
                conn, provider="google", file_name=name, size_bytes=size, subject_id=subject_id
            )
            row = fetch_one(conn, "SELECT * FROM archive_files WHERE id = %s", (fid,))
        stats["discovered"] += 1
        if _should_skip_row(row, dest_dir / name, dest_dir):
            continue

        local = dest_dir / name
        if local.is_file():
            if not _file_stable(local, poll):
                continue
            stats["stable"] += 1
            if not disk_guard.ensure_disk_for_zip(f"sync {name}", size):
                break
            digest = ledger.sha256_file(local)
            with transaction() as conn:
                existing = ledger.get_by_sha256(conn, digest)
                if existing:
                    canon = existing.get("file_path") or str(local)
                    ledger.mark_downloaded(conn, fid, digest, canon)
                else:
                    ledger.mark_downloaded(conn, fid, digest, str(local))
            stats["downloaded"] += 1
            continue

        if not disk_guard.ensure_disk_for_zip(f"download {name}", size):
            break
        ok, copy_err = _rclone_copy_one(name, local)
        if not ok:
            msg = copy_err or "rclone copy failed"
            stats["errors"].append(msg)
            run_ok = False
            with transaction() as conn:
                ledger.set_state(conn, fid, "failed", error=msg)
            continue
        if not _file_stable(local, poll):
            continue
        stats["stable"] += 1
        digest = ledger.sha256_file(local)
        with transaction() as conn:
            ledger.mark_downloaded(conn, fid, digest, str(local))
        stats["downloaded"] += 1
        notify_user(
            f"Archive sync: downloaded {name} ({size / (1024**2):.1f} MB). "
            f"Ledger state=downloaded.",
            category=NotifyCategory.ARCHIVE_DOWNLOAD,
            severity=NotifySeverity.INFO,
        )

    for provider, inbox in (("meta", paths.inbox_meta()), ("manual", paths.inbox_manual())):
        for name, size in scan_inbox_local(provider, inbox):
            with transaction() as conn:
                ledger.upsert_discovered(
                    conn,
                    provider=provider,
                    file_name=name,
                    size_bytes=size,
                    file_path=str(inbox / name),
                    subject_id=subject_id,
                )
            stats["discovered"] += 1

    with transaction() as conn:
        ledger.finish_run(conn, run_id, run_ok, stats)
    return {"ok": run_ok, **stats}


def sync_loop_forever() -> None:
    settings = get_settings()
    log.info("archive_sync_loop_started", poll=settings.archive_sync_poll_seconds)
    while True:
        try:
            sync_drive_and_inbox()
        except Exception as exc:
            log.exception("archive_sync_loop_error", error=str(exc))
        time.sleep(settings.archive_sync_poll_seconds)
