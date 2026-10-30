from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "archive"


@pytest.fixture
def archive_data_tmp(tmp_path, monkeypatch):
    root = tmp_path / "life-archive-data"
    for sub in ("inbox/google", "inbox/meta", "inbox/manual", "staging", "olap", "datasets"):
        (root / sub).mkdir(parents=True)
    monkeypatch.setenv("VSM_ARCHIVE_DATA_ROOT", str(root))
    from virtual_self.config import get_settings

    get_settings.cache_clear()
    return root


def _mini_takeout_zip(path: Path) -> None:
    chrome = {
        "Browser History": [
            {"url": "https://example.com/recipes", "title": "Recipes", "time_usec": 1609459200000000}
        ]
    }
    activity = [{"title": "Searched for pasta", "time": "2020-01-02T10:00:00.000Z"}]
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Takeout/Chrome/History.json", json.dumps(chrome))
        zf.writestr("Takeout/My Activity/Search/MyActivity.json", json.dumps(activity))


def test_zip_slip_rejected(tmp_path, monkeypatch):
    from virtual_self.archive.unpack import safe_unpack

    monkeypatch.setattr("virtual_self.archive.unpack.disk_guard.ensure_disk_or_notify", lambda *_: True)
    bad = tmp_path / "bad.zip"
    good_dir = tmp_path / "out"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("../evil.txt", "x")
    n, _ = safe_unpack(bad, good_dir)
    assert n == 0
    assert not (tmp_path / "evil.txt").exists()


def test_zip_bomb_ratio_skipped(tmp_path, monkeypatch):
    from virtual_self.archive.unpack import safe_unpack

    monkeypatch.setattr("virtual_self.archive.unpack.disk_guard.ensure_disk_or_notify", lambda *_: True)
    monkeypatch.setattr("virtual_self.archive.unpack.MAX_COMPRESSION_RATIO", 10)
    zpath = tmp_path / "bomb.zip"
    with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("huge.txt", "A" * 50000)
    n, _ = safe_unpack(zpath, tmp_path / "staging")
    assert n == 0


def test_chrome_parser():
    from virtual_self.archive.parsers.chrome import parse_file

    p = FIX / "chrome_history.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"Browser History": [{"url": "https://a.com", "title": "A", "time_usec": 1}]}),
        encoding="utf-8",
    )
    evs = parse_file(p, "Takeout/Chrome/History.json", "google", "chrome")
    assert len(evs) == 1
    assert evs[0].domain == "a.com"


def test_policy_excludes_finance():
    from virtual_self.archive.parsers.base import ArchiveEvent, new_event_id
    from virtual_self.archive.policy import apply_policy

    ev = ArchiveEvent(
        event_id=new_event_id(),
        provider="google",
        product="mail",
        event_type="email_received",
        ts=None,
        actor="other",
        text="Your OTP code is 123456",
    )
    kept, skipped = apply_policy([ev])
    assert len(kept) == 0
    assert skipped.get("finance_excluded", 0) == 1


def test_verifier_rejects_contradiction():
    from virtual_self.archive.llm.pipeline import verify_candidate

    cand = {
        "content": "I never cook at home",
        "confidence": 0.95,
        "evidence_event_ids": ["e1"],
    }
    events = [{"event_id": "e1", "title": "Cooking class signup", "text": "Enrolled in cooking class"}]

    def fake_fetch(ids):
        return events if ids == ["e1"] else []

    def fake_chat(system, user, temperature=0.0):
        return {"verdict": "unsupported", "reason": "contradicts evidence"}

    with (
        patch("virtual_self.archive.llm.pipeline.fetch_events_by_ids", fake_fetch),
        patch("virtual_self.archive.llm.pipeline.chat_json", fake_chat),
        patch("virtual_self.config.get_settings") as gs,
    ):
        gs.return_value.archive_llm_enabled = True
        out = verify_candidate(cand, None)
    assert out["verdict"] == "unsupported"
    assert out["confidence"] == 0.0


def test_dataset_scrub_pii():
    from virtual_self.archive.dataset import scrub_pii

    t = scrub_pii("Email alice@example.com about Bob Smith")
    assert "[EMAIL]" in t
    assert "alice@example.com" not in t


def test_olap_product_counts_match_parquet(archive_data_tmp):
    from datetime import UTC, datetime

    from virtual_self.archive.olap import (
        olap_refresh,
        parquet_product_counts,
        product_counts,
        write_events_parquet,
    )
    from virtual_self.archive.parsers.base import ArchiveEvent, new_event_id

    events = [
        ArchiveEvent(
            event_id=new_event_id(),
            provider="google",
            product="chrome",
            event_type="visit",
            ts=datetime(2020, 1, 1, tzinfo=UTC),
            actor="self",
            title="A",
        ),
        ArchiveEvent(
            event_id=new_event_id(),
            provider="google",
            product="contacts",
            event_type="contact",
            ts=datetime(2020, 1, 2, tzinfo=UTC),
            actor="self",
            title="B",
        ),
    ]
    write_events_parquet(events, "mix1")
    assert product_counts() == {"chrome": 1, "contacts": 1}
    assert parquet_product_counts() == product_counts()
    out = olap_refresh()
    assert out["matches_disk"] is True
    assert out["product_counts"] == {"chrome": 1, "contacts": 1}


def test_mini_takeout_e2e(ensure_db, archive_data_tmp, auth_headers):
    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.archive import ledger
    from virtual_self.archive.run import process_archive_file
    from virtual_self.persistence.db import fetch_one, init_pool, transaction

    init_pool()
    auth = resolve_auth(auth_headers["Authorization"])
    import uuid

    zip_path = archive_data_tmp / "inbox" / "manual" / f"mini-{uuid.uuid4().hex[:8]}.zip"
    _mini_takeout_zip(zip_path)
    digest = ledger.sha256_file(zip_path)
    with transaction() as conn:
        fid = ledger.upsert_discovered(
            conn,
            provider="manual",
            file_name=zip_path.name,
            size_bytes=zip_path.stat().st_size,
            file_path=str(zip_path),
            subject_id=auth.subject_id,
        )
        ledger.mark_downloaded(conn, fid, digest, str(zip_path))
        row = fetch_one(conn, "SELECT * FROM archive_files WHERE id = %s", (fid,))

    with (
        patch.dict(os.environ, {"VSM_ARCHIVE_LLM_ENABLED": "false"}),
        patch("virtual_self.archive.unpack.disk_guard.ensure_disk_or_notify", lambda *_: True),
        patch("virtual_self.archive.run.notify_user", lambda *_a, **_k: True),
    ):
        from virtual_self.config import get_settings

        get_settings.cache_clear()
        out = process_archive_file(auth, row, analyze=False, promote=True)

    assert out["ok"] is True
    assert out["events_parsed"] >= 1
    assert out.get("events_promoted", 0) >= 1
    assert out.get("archive_memories_after", 0) >= out.get("archive_memories_before", 0) + 1


def test_archive_events_queryable_in_compile_context(ensure_db, archive_data_tmp, auth_headers):
    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.archive import ledger
    from virtual_self.archive.run import process_archive_file
    from virtual_self.persistence.db import fetch_one, init_pool, transaction
    from virtual_self.services.personality import bootstrap_personality, compile_context

    init_pool()
    auth = resolve_auth(auth_headers["Authorization"])
    bootstrap_personality(auth)
    import uuid

    zip_path = archive_data_tmp / "inbox" / "manual" / f"ctx-{uuid.uuid4().hex[:8]}.zip"
    _mini_takeout_zip(zip_path)
    digest = ledger.sha256_file(zip_path)
    with transaction() as conn:
        fid = ledger.upsert_discovered(
            conn,
            provider="manual",
            file_name=zip_path.name,
            size_bytes=zip_path.stat().st_size,
            file_path=str(zip_path),
            subject_id=auth.subject_id,
        )
        ledger.mark_downloaded(conn, fid, digest, str(zip_path))
        row = fetch_one(conn, "SELECT * FROM archive_files WHERE id = %s", (fid,))

    with (
        patch.dict(os.environ, {"VSM_ARCHIVE_LLM_ENABLED": "false"}),
        patch("virtual_self.archive.unpack.disk_guard.ensure_disk_or_notify", lambda *_: True),
        patch("virtual_self.archive.run.notify_user", lambda *_a, **_k: True),
    ):
        from virtual_self.config import get_settings

        get_settings.cache_clear()
        process_archive_file(auth, row, analyze=False, promote=True)

    bundle = compile_context(auth, query="Recipes pasta", memory_limit=12)
    contents = " ".join(m.get("content", "") for m in bundle["relevant_memories"])
    assert "Recipes" in contents or "pasta" in contents.lower()


def test_promote_blocked_still_promotes_olap_events(ensure_db, archive_data_tmp, auth_headers):
    from virtual_self.archive.flow import StagingParseResult
    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.archive import ledger
    from virtual_self.archive.run import process_archive_file
    from virtual_self.persistence.db import fetch_one, init_pool, transaction

    init_pool()
    auth = resolve_auth(auth_headers["Authorization"])
    import uuid

    zip_path = archive_data_tmp / "inbox" / "manual" / f"blk-{uuid.uuid4().hex[:8]}.zip"
    _mini_takeout_zip(zip_path)
    digest = ledger.sha256_file(zip_path)
    with transaction() as conn:
        fid = ledger.upsert_discovered(
            conn,
            provider="manual",
            file_name=zip_path.name,
            size_bytes=zip_path.stat().st_size,
            file_path=str(zip_path),
            subject_id=auth.subject_id,
        )
        ledger.mark_downloaded(conn, fid, digest, str(zip_path))
        row = fetch_one(conn, "SELECT * FROM archive_files WHERE id = %s", (fid,))

    real_classify = None

    def fake_classify(*args, **kwargs):
        out = real_classify(*args, **kwargs)
        return StagingParseResult(
            files_seen=out.files_seen,
            events=out.events,
            unknown_samples=out.unknown_samples,
            decisions=out.decisions,
            quarantined=out.quarantined,
            supervisor_meta=out.supervisor_meta,
            block_promote=True,
        )

    with (
        patch.dict(os.environ, {"VSM_ARCHIVE_LLM_ENABLED": "false", "VSM_ARCHIVE_SUPERVISOR_ENABLED": "false"}),
        patch("virtual_self.archive.unpack.disk_guard.ensure_disk_or_notify", lambda *_: True),
        patch("virtual_self.archive.run.notify_user", lambda *_a, **_k: True),
    ):
        from virtual_self.archive import flow as flow_mod
        from virtual_self.config import get_settings

        get_settings.cache_clear()
        real_classify = flow_mod.classify_and_parse_staging
        with patch("virtual_self.archive.run.classify_and_parse_staging", fake_classify):
            out = process_archive_file(auth, row, analyze=False, promote=True)

    assert out.get("llm_promote_skipped") == "specialist_quarantine"
    assert out.get("events_promoted", 0) >= 1


def test_promote_backfill_idempotent(ensure_db, archive_data_tmp, auth_headers):
    from virtual_self.auth.credentials import resolve_auth
    from virtual_self.archive import ledger
    from virtual_self.archive.promote import promote_backfill_from_parquet
    from virtual_self.archive.run import process_archive_file
    from virtual_self.persistence.db import fetch_one, init_pool, transaction

    init_pool()
    auth = resolve_auth(auth_headers["Authorization"])
    import uuid

    zip_path = archive_data_tmp / "inbox" / "manual" / f"bf-{uuid.uuid4().hex[:8]}.zip"
    _mini_takeout_zip(zip_path)
    digest = ledger.sha256_file(zip_path)
    with transaction() as conn:
        fid = ledger.upsert_discovered(
            conn,
            provider="manual",
            file_name=zip_path.name,
            size_bytes=zip_path.stat().st_size,
            file_path=str(zip_path),
            subject_id=auth.subject_id,
        )
        ledger.mark_downloaded(conn, fid, digest, str(zip_path))
        row = fetch_one(conn, "SELECT * FROM archive_files WHERE id = %s", (fid,))

    with (
        patch.dict(os.environ, {"VSM_ARCHIVE_LLM_ENABLED": "false", "VSM_ARCHIVE_SUPERVISOR_ENABLED": "false"}),
        patch("virtual_self.archive.unpack.disk_guard.ensure_disk_or_notify", lambda *_: True),
        patch("virtual_self.archive.run.notify_user", lambda *_a, **_k: True),
    ):
        from virtual_self.config import get_settings

        get_settings.cache_clear()
        process_archive_file(auth, row, analyze=False, promote=True)

    first = promote_backfill_from_parquet(auth, dry_run=False)
    second = promote_backfill_from_parquet(auth, dry_run=False)
    assert second["archive_memories_after"] == first["archive_memories_after"]
    assert second["events_promoted"] == 0
    assert second["events_duplicate"] >= 1
