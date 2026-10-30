from __future__ import annotations

import uuid

import pytest

from virtual_self.archive import ledger, reconcile
from virtual_self.persistence.db import fetch_one, init_pool, transaction


@pytest.fixture
def archive_data_tmp(tmp_path, monkeypatch):
    root = tmp_path / "life-archive-data"
    for sub in ("inbox/google", "inbox/meta", "inbox/manual", "staging", "olap", "datasets"):
        (root / sub).mkdir(parents=True)
    monkeypatch.setenv("VSM_ARCHIVE_DATA_ROOT", str(root))
    from virtual_self.config import get_settings

    get_settings.cache_clear()
    return root


def _insert_row(
    conn,
    *,
    file_name: str,
    state: str = "downloaded",
    file_path: str | None = "/nonexistent/path.zip",
    provider: str = "google",
) -> uuid.UUID:
    fid = ledger.upsert_discovered(
        conn,
        provider=provider,
        file_name=file_name,
        size_bytes=0,
        file_path=file_path,
    )
    ledger.set_state(conn, fid, state)
    return fid


def test_phantom_reason_patterns():
    assert reconcile.phantom_reason({"file_name": "takeout-001.zip", "state": "stable"})
    assert reconcile.phantom_reason({"file_name": "mini-abc.zip", "state": "stable"})
    assert reconcile.phantom_reason({"file_name": "takeout-remote-deadbeef.zip", "state": "stable"})
    assert reconcile.phantom_reason(
        {"file_name": "real.zip", "file_path": None, "provider": "google", "state": "stable"}
    )
    assert (
        reconcile.phantom_reason(
            {
                "file_name": "Takeout-2024.zip",
                "file_path": "/data/Takeout-2024.zip",
                "state": "stable",
            }
        )
        is None
    )


def test_phantom_null_path_with_inbox_zip(ensure_db, archive_data_tmp):
    init_pool()
    zip_path = archive_data_tmp / "inbox" / "google" / "real-takeout.zip"
    zip_path.write_bytes(b"PK")
    with transaction() as conn:
        fid = _insert_row(conn, file_name="real-takeout.zip", file_path=None, state="stable")
        row = fetch_one(conn, "SELECT * FROM archive_files WHERE id = %s", (fid,))
    assert reconcile.phantom_reason(row) is None


@pytest.mark.parametrize(
    "file_name",
    ["takeout-001.zip", "mini-test.zip", "takeout-remote-abc.zip"],
)
def test_reconcile_marks_phantom_failed(ensure_db, file_name: str):
    init_pool()
    with transaction() as conn:
        fid = _insert_row(conn, file_name=file_name, state="downloaded")
        stats = reconcile.reconcile_phantoms(conn, dry_run=False, delete=False)
        row = fetch_one(conn, "SELECT state, error_message FROM archive_files WHERE id = %s", (fid,))
    assert stats["phantom_marked_failed"] >= 1
    assert row["state"] == "failed"
    assert "reconcile:" in (row["error_message"] or "")


def test_reconcile_dry_run_no_writes(ensure_db):
    init_pool()
    with transaction() as conn:
        fid = _insert_row(conn, file_name="takeout-001.zip", state="stable")
        stats = reconcile.reconcile_phantoms(conn, dry_run=True, delete=False)
        row = fetch_one(conn, "SELECT state FROM archive_files WHERE id = %s", (fid,))
    assert stats["phantoms_found"] >= 1
    assert stats["phantom_marked_failed"] == 0
    assert row["state"] == "stable"


def test_reconcile_delete_requires_live_run(ensure_db):
    init_pool()
    with transaction() as conn:
        fid = _insert_row(conn, file_name="mini-gone.zip", state="discovered", file_path=None)
        stats = reconcile.reconcile_phantoms(conn, dry_run=True, delete=True)
        row = fetch_one(conn, "SELECT id FROM archive_files WHERE id = %s", (fid,))
    assert stats["phantom_deleted"] == 0
    assert row is not None


def test_reconcile_delete_removes_row(ensure_db):
    init_pool()
    unique = f"takeout-remote-delete-{uuid.uuid4().hex[:8]}.zip"
    with transaction() as conn:
        fid = _insert_row(conn, file_name=unique, state="discovered")
        reconcile.reconcile_phantoms(conn, dry_run=False, delete=True)
        row = fetch_one(conn, "SELECT id FROM archive_files WHERE id = %s", (fid,))
    assert row is None


def test_reconcile_skips_done_and_failed(ensure_db):
    init_pool()
    with transaction() as conn:
        done_id = _insert_row(conn, file_name="mini-done.zip", state="done")
        failed_id = _insert_row(conn, file_name="mini-failed.zip", state="failed")
        stats = reconcile.reconcile_phantoms(conn, dry_run=False, delete=False)
        done = fetch_one(conn, "SELECT state FROM archive_files WHERE id = %s", (done_id,))
        failed = fetch_one(conn, "SELECT state FROM archive_files WHERE id = %s", (failed_id,))
    assert done["state"] == "done"
    assert failed["state"] == "failed"
    assert stats["phantom_marked_failed"] == 0
