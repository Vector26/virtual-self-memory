from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest


@pytest.fixture
def archive_flow_env(tmp_path, monkeypatch):
    root = tmp_path / "life-archive-data"
    for sub in ("inbox/google", "inbox/meta", "inbox/manual", "staging", "olap", "datasets"):
        (root / sub).mkdir(parents=True)
    monkeypatch.setenv("VSM_ARCHIVE_DATA_ROOT", str(root))
    monkeypatch.setenv("VSM_ARCHIVE_STREAMING", "true")
    monkeypatch.setenv("VSM_ARCHIVE_DELETE_ZIP_AFTER", "true")
    monkeypatch.setenv("VSM_ARCHIVE_SUPERVISOR_ENABLED", "true")
    from virtual_self.config import get_settings

    get_settings.cache_clear()
    return root


def _write_zip(path: Path, name: str = "Takeout/x.json") -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            name,
            json.dumps({"Browser History": [{"url": "https://x.com", "title": "X", "time_usec": 1}]}),
        )


def test_sync_one_marks_stable_local(ensure_db, archive_flow_env, monkeypatch):
    from virtual_self.archive import ledger
    from virtual_self.archive.sync import sync_one_next_stable_zip
    from virtual_self.persistence.db import fetch_one, init_pool, transaction

    init_pool()
    inbox = archive_flow_env / "inbox" / "google"
    zname = f"takeout-local-{uuid4().hex[:8]}.zip"
    zpath = inbox / zname
    _write_zip(zpath)

    remote = [{"Name": zname, "Size": zpath.stat().st_size}]

    monkeypatch.setattr("virtual_self.archive.sync._rclone_lsf", lambda _p: (remote, None))
    monkeypatch.setattr("virtual_self.archive.sync._file_stable", lambda _p, _s: True)
    monkeypatch.setattr("virtual_self.archive.sync._rclone_copy_one", lambda *_: (True, None))
    monkeypatch.setattr("virtual_self.archive.disk_guard.ensure_disk_for_zip", lambda *_a, **_k: True)
    monkeypatch.setattr("virtual_self.archive.disk_guard.ensure_disk_or_notify", lambda *_: True)

    out = sync_one_next_stable_zip()
    assert out["action"] == "marked_downloaded"
    assert out["downloaded"] == 1

    with transaction() as conn:
        row = fetch_one(
            conn,
            "SELECT state FROM archive_files WHERE file_name = %s",
            (zname,),
        )
    assert row["state"] == "downloaded"


def test_sync_one_downloads_single_remote(ensure_db, archive_flow_env, monkeypatch):
    from virtual_self.archive.sync import sync_one_next_stable_zip

    from virtual_self.persistence.db import init_pool

    init_pool()
    inbox = archive_flow_env / "inbox" / "google"
    zname = f"takeout-remote-{uuid4().hex[:8]}.zip"
    remote = [{"Name": zname, "Size": 1024}]
    copies: list[str] = []

    def fake_copy(name: str, dest: Path) -> tuple[bool, str | None]:
        copies.append(name)
        _write_zip(dest)
        return True, None

    monkeypatch.setattr("virtual_self.archive.sync._rclone_lsf", lambda _p: (remote, None))
    monkeypatch.setattr("virtual_self.archive.sync._file_stable", lambda _p, _s: True)
    monkeypatch.setattr("virtual_self.archive.sync._rclone_copy_one", fake_copy)
    monkeypatch.setattr("virtual_self.archive.disk_guard.ensure_disk_for_zip", lambda *_a, **_k: True)
    monkeypatch.setattr("virtual_self.archive.disk_guard.ensure_disk_or_notify", lambda *_: True)

    out = sync_one_next_stable_zip()
    assert copies == [zname]
    assert out["action"] == "downloaded"
    assert (inbox / zname).is_file()


def test_run_flow_cycle_mocked(ensure_db, archive_flow_env, auth_headers, monkeypatch):
    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.archive.run import run_flow
    from virtual_self.persistence.db import init_pool

    init_pool()
    auth = resolve_auth(auth_headers["Authorization"])
    calls = {"sync": 0, "process": 0}

    def fake_sync(_sid=None):
        calls["sync"] += 1
        if calls["sync"] == 1:
            return {"ok": True, "action": "downloaded", "downloaded": 1, "file_name": "a.zip"}
        return {"ok": True, "action": "none", "downloaded": 0}

    def fake_process(auth, row, *, analyze=True, promote=True):
        calls["process"] += 1
        return {"ok": True, "events_parsed": 3, "events_by_product": {"chrome": 3}}

    pending = [
        {"id": uuid4(), "file_name": "a.zip", "state": "downloaded", "provider": "google", "file_path": ""},
        None,
        None,
    ]

    def fake_next(conn):
        return pending.pop(0) if pending else None

    monkeypatch.setattr("virtual_self.archive.sync.sync_one_next_stable_zip", fake_sync)
    monkeypatch.setattr("virtual_self.archive.run.process_archive_file", fake_process)
    monkeypatch.setattr("virtual_self.archive.run._next_work_row", fake_next)
    monkeypatch.setattr("virtual_self.archive.run.ledger.reconcile_ledger", lambda *a, **k: {})
    monkeypatch.setattr("virtual_self.archive.run.notify_user", lambda *_a, **_k: None)
    monkeypatch.setattr("virtual_self.archive.run.product_counts", lambda: {"chrome": 3})

    summary = run_flow(auth, analyze=False, promote=False)
    assert calls["process"] == 1
    assert len(summary["cycles"]) == 1
    assert summary["cycles"][0]["events_parsed"] == 3


def test_run_flow_continues_after_zip_missing(ensure_db, archive_flow_env, auth_headers, monkeypatch):
    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.archive.run import run_flow
    from virtual_self.persistence.db import init_pool

    init_pool()
    auth = resolve_auth(auth_headers["Authorization"])
    calls = {"process": 0}

    def fake_sync(_sid=None):
        return {"ok": True, "action": "none", "downloaded": 0, "reason": "no_remote_zips"}

    def fake_process(auth, row, *, analyze=True, promote=True):
        calls["process"] += 1
        if row["file_name"] == "phantom.zip":
            return {"ok": False, "error": "zip missing"}
        return {"ok": True, "events_parsed": 1, "events_by_product": {"chrome": 1}}

    rows = [
        {"id": uuid4(), "file_name": "phantom.zip", "state": "downloaded", "provider": "google", "file_path": ""},
        {"id": uuid4(), "file_name": "good.zip", "state": "downloaded", "provider": "google", "file_path": ""},
        None,
    ]

    def fake_next(conn):
        return rows.pop(0) if rows else None

    monkeypatch.setattr("virtual_self.archive.sync.sync_one_next_stable_zip", fake_sync)
    monkeypatch.setattr("virtual_self.archive.run.process_archive_file", fake_process)
    monkeypatch.setattr("virtual_self.archive.run._next_work_row", fake_next)
    monkeypatch.setattr("virtual_self.archive.run.ledger.reconcile_ledger", lambda *a, **k: {})
    monkeypatch.setattr("virtual_self.archive.run.notify_user", lambda *_a, **_k: None)
    monkeypatch.setattr("virtual_self.archive.run.product_counts", lambda: {"chrome": 1})

    summary = run_flow(auth, analyze=False, promote=False)
    assert calls["process"] == 2
    assert len(summary["cycles"]) == 2
    assert summary["cycles"][0]["error"] == "zip missing"
    assert summary["cycles"][1]["ok"] is True


def test_run_flow_stops_on_disk_guard(ensure_db, archive_flow_env, auth_headers, monkeypatch):
    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.archive.run import run_flow
    from virtual_self.persistence.db import init_pool

    init_pool()
    auth = resolve_auth(auth_headers["Authorization"])

    monkeypatch.setattr(
        "virtual_self.archive.sync.sync_one_next_stable_zip",
        lambda _sid=None: {"ok": False, "reason": "disk_guard", "action": "none", "downloaded": 0},
    )
    monkeypatch.setattr("virtual_self.archive.run._next_work_row", lambda _c: None)
    monkeypatch.setattr("virtual_self.archive.run.ledger.reconcile_ledger", lambda *a, **k: {})
    monkeypatch.setattr("virtual_self.archive.run.notify_user", lambda *_a, **_k: None)
    monkeypatch.setattr("virtual_self.archive.run.product_counts", lambda: {})

    summary = run_flow(auth, analyze=False, promote=False)
    assert summary["stopped_reason"] == "disk_guard"
    assert summary["cycles"] == []


def test_run_flow_stops_on_auth_fatal(ensure_db, archive_flow_env, auth_headers, monkeypatch):
    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.archive.run import run_flow
    from virtual_self.persistence.db import init_pool

    init_pool()
    auth = resolve_auth(auth_headers["Authorization"])
    row = {
        "id": uuid4(),
        "file_name": "a.zip",
        "state": "downloaded",
        "provider": "google",
        "file_path": "",
    }

    monkeypatch.setattr(
        "virtual_self.archive.sync.sync_one_next_stable_zip",
        lambda _sid=None: {"ok": True, "action": "none", "downloaded": 0},
    )
    monkeypatch.setattr("virtual_self.archive.run._next_work_row", lambda _c: row)
    monkeypatch.setattr("virtual_self.archive.run.ledger.reconcile_ledger", lambda *a, **k: {})
    monkeypatch.setattr(
        "virtual_self.archive.run.process_archive_file",
        lambda *_a, **_k: {"ok": False, "error": "forbidden", "fatal": "auth"},
    )
    monkeypatch.setattr("virtual_self.archive.run.notify_user", lambda *_a, **_k: None)
    monkeypatch.setattr("virtual_self.archive.run.product_counts", lambda: {})

    summary = run_flow(auth, analyze=False, promote=False)
    assert summary["stopped_reason"] == "auth"
    assert len(summary["cycles"]) == 1


def test_run_flow_max_consecutive_process_failures(ensure_db, archive_flow_env, auth_headers, monkeypatch):
    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.archive.run import _MAX_CONSECUTIVE_HARD_FAILURES, run_flow
    from virtual_self.persistence.db import init_pool

    init_pool()
    auth = resolve_auth(auth_headers["Authorization"])
    row = {
        "id": uuid4(),
        "file_name": "bad.zip",
        "state": "downloaded",
        "provider": "google",
        "file_path": "",
    }

    monkeypatch.setattr(
        "virtual_self.archive.sync.sync_one_next_stable_zip",
        lambda _sid=None: {"ok": True, "action": "none", "downloaded": 0},
    )
    monkeypatch.setattr("virtual_self.archive.run._next_work_row", lambda _c: row)
    monkeypatch.setattr("virtual_self.archive.run.ledger.reconcile_ledger", lambda *a, **k: {})
    monkeypatch.setattr(
        "virtual_self.archive.run.process_archive_file",
        lambda *_a, **_k: {"ok": False, "error": "parse blew up"},
    )
    monkeypatch.setattr("virtual_self.archive.run.notify_user", lambda *_a, **_k: None)
    monkeypatch.setattr("virtual_self.archive.run.product_counts", lambda: {})

    summary = run_flow(auth, analyze=False, promote=False)
    assert len(summary["cycles"]) == _MAX_CONSECUTIVE_HARD_FAILURES
    assert summary["stopped_reason"] == "too_many_failures:parse blew up"


def test_supervisor_stub_falls_back_to_detect(archive_flow_env, monkeypatch):
    from virtual_self.archive.detect import rough_classify
    from virtual_self.archive.flow import build_parse_decisions
    from virtual_self.config import get_settings

    monkeypatch.setenv("VSM_ARCHIVE_SUPERVISOR_ENABLED", "false")
    get_settings.cache_clear()
    staging = archive_flow_env / "staging" / "stub"
    staging.mkdir(parents=True)
    chrome = staging / "Takeout" / "Chrome"
    chrome.mkdir(parents=True)
    (chrome / "History.json").write_text('{"Browser History": []}', encoding="utf-8")

    files = rough_classify(staging, "google")
    decisions, quarantined, meta, block = build_parse_decisions(files)
    assert meta["skipped"] is True
    assert meta["reason"] == "disabled"
    assert quarantined == []
    assert block is False
    assert any(d.should_parse and d.detected.product == "chrome" for d in decisions)


def test_supervisor_plan_filters_parse_queue(archive_flow_env, monkeypatch):
    from virtual_self.archive.detect import rough_classify
    from virtual_self.archive.flow import build_parse_decisions
    from virtual_self.archive.llm.supervisor import SupervisorAssignment, SupervisorPlan
    from virtual_self.config import get_settings

    monkeypatch.setenv("VSM_ARCHIVE_SUPERVISOR_ENABLED", "true")
    get_settings.cache_clear()
    staging = archive_flow_env / "staging" / "plan"
    staging.mkdir(parents=True)
    chrome = staging / "Takeout" / "Chrome"
    chrome.mkdir(parents=True)
    (chrome / "History.json").write_text(
        json.dumps(
            {"Browser History": [{"url": "https://x.com", "title": "X", "time_usec": 1}]}
        ),
        encoding="utf-8",
    )

    files = rough_classify(staging, "google")
    chrome_files = [f for f in files if f.product == "chrome"]
    assert chrome_files

    def fake_plan(_files, archive_run_id=None):
        return SupervisorPlan(
            assignments=[
                SupervisorAssignment(
                    specialist_id="chrome",
                    file_rel_paths=[f.rel_path for f in chrome_files],
                )
            ]
        )

    def fake_classify(assignment, sample, detected, use_llm=True):
        return {
            "parse_strategy": "json_nested",
            "event_type_hints": ["browse"],
            "pii_risk": "medium",
            "should_parse": True,
            "confidence": 0.95,
            "notes": "mock",
        }

    monkeypatch.setattr("virtual_self.archive.flow.supervisor_available", lambda: True)
    monkeypatch.setattr("virtual_self.archive.flow.supervisor_plan", fake_plan)
    monkeypatch.setattr("virtual_self.archive.flow.classify_file", fake_classify)

    decisions, quarantined, meta, block = build_parse_decisions(files)
    assert meta["skipped"] is False
    assert quarantined == []
    assert block is False
    assert all(d.should_parse for d in decisions if d.detected.product == "chrome")


def test_specialist_failure_quarantines(archive_flow_env, monkeypatch):
    from virtual_self.archive.detect import rough_classify
    from virtual_self.archive.flow import build_parse_decisions
    from virtual_self.archive.llm.supervisor import SupervisorAssignment, SupervisorPlan
    from virtual_self.config import get_settings

    monkeypatch.setenv("VSM_ARCHIVE_SUPERVISOR_ENABLED", "true")
    get_settings.cache_clear()
    staging = archive_flow_env / "staging" / "fail"
    staging.mkdir(parents=True)
    chrome = staging / "Takeout" / "Chrome"
    chrome.mkdir(parents=True)
    (chrome / "History.json").write_text("{}", encoding="utf-8")
    files = rough_classify(staging, "google")
    rel = [f for f in files if f.product == "chrome"][0].rel_path

    monkeypatch.setattr(
        "virtual_self.archive.flow.supervisor_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "virtual_self.archive.flow.supervisor_plan",
        lambda _f, archive_run_id=None: SupervisorPlan(
            assignments=[SupervisorAssignment(specialist_id="chrome", file_rel_paths=[rel])]
        ),
    )
    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr("virtual_self.archive.flow.classify_file", boom)

    decisions, quarantined, _meta, block = build_parse_decisions(files)
    assert rel in quarantined
    assert block is True
    assert all(not d.should_parse for d in decisions if d.detected.rel_path == rel)


def test_rclone_lsf_retries_then_lists(archive_flow_env, monkeypatch, tmp_path):
    import subprocess

    from virtual_self.archive import sync as sync_mod
    from virtual_self.config import get_settings

    cfg = tmp_path / "rclone.conf"
    cfg.write_text("[vault]\ntype = local\n", encoding="utf-8")
    monkeypatch.setenv("VSM_ARCHIVE_RCLONE_CONFIG", str(cfg))
    get_settings.cache_clear()
    calls = {"n": 0}

    class _Proc:
        stdout = '[{"Name": "a.zip", "Size": 1, "IsDir": false}]'
        returncode = 0

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise subprocess.CalledProcessError(1, cmd, stderr="transient")
        return _Proc()

    monkeypatch.setattr(sync_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(sync_mod, "_rclone_backoff", lambda _a: None)

    items, err = sync_mod._rclone_lsf("Takeout")
    assert err is None
    assert items[0]["Name"] == "a.zip"
    assert calls["n"] == 3


def test_sync_lsf_failure_recorded_in_ledger(ensure_db, archive_flow_env, monkeypatch):
    from virtual_self.archive.sync import sync_one_next_stable_zip
    from virtual_self.persistence.db import fetch_one, init_pool, transaction

    init_pool()
    monkeypatch.setattr(
        "virtual_self.archive.sync._rclone_lsf",
        lambda _p: ([], "lsf exhausted"),
    )
    monkeypatch.setattr("virtual_self.archive.disk_guard.ensure_disk_or_notify", lambda *_: True)
    monkeypatch.setattr("virtual_self.archive.sync.notify_user", lambda *_a, **_k: None)

    out = sync_one_next_stable_zip()
    assert out["ok"] is False
    assert out["reason"] == "rclone_lsf_failed"
    assert out["errors"] == ["lsf exhausted"]

    with transaction() as conn:
        row = fetch_one(
            conn,
            """
            SELECT state, stats FROM archive_runs
            WHERE run_type = 'archive.sync'
            ORDER BY started_at DESC
            LIMIT 1
            """,
        )
    assert row["state"] == "failed"
    assert row["stats"]["reason"] == "rclone_lsf_failed"


def test_sync_copy_failure_marks_file_failed(ensure_db, archive_flow_env, monkeypatch):
    from virtual_self.archive.sync import sync_one_next_stable_zip
    from virtual_self.persistence.db import fetch_one, init_pool, transaction

    init_pool()
    zname = f"fail-{uuid4().hex[:8]}.zip"
    remote = [{"Name": zname, "Size": 1024}]
    monkeypatch.setattr("virtual_self.archive.sync._rclone_lsf", lambda _p: (remote, None))
    monkeypatch.setattr("virtual_self.archive.sync._file_stable", lambda _p, _s: True)
    monkeypatch.setattr(
        "virtual_self.archive.sync._rclone_copy_one",
        lambda *_: (False, "copy exhausted"),
    )
    monkeypatch.setattr("virtual_self.archive.disk_guard.ensure_disk_for_zip", lambda *_a, **_k: True)
    monkeypatch.setattr("virtual_self.archive.disk_guard.ensure_disk_or_notify", lambda *_: True)

    out = sync_one_next_stable_zip()
    assert out["action"] == "download_failed"
    assert out["errors"] == ["copy exhausted"]

    with transaction() as conn:
        row = fetch_one(
            conn,
            "SELECT state, error_message FROM archive_files WHERE file_name = %s",
            (zname,),
        )
    assert row["state"] == "failed"
    assert row["error_message"] == "copy exhausted"


def test_disk_guard_zip_headroom(monkeypatch):
    from virtual_self.archive import disk_guard

    class _S:
        archive_min_free_gb = 15.0

    monkeypatch.setattr(disk_guard, "free_bytes", lambda _p="/": 20 * 1024**3)
    monkeypatch.setattr(disk_guard, "get_settings", lambda: _S())
    monkeypatch.setattr(disk_guard, "notify_user", lambda *_a, **_k: None)
    assert disk_guard.ensure_disk_for_zip("t", 5 * 1024**3) is True
    assert disk_guard.ensure_disk_for_zip("t", 9 * 1024**3) is False
