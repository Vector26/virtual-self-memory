"""T-7: temporal QA eval scenarios, metrics, and harness."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from uuid6 import uuid7

from virtual_self.auth.credentials import resolve_auth
from virtual_self.eval.harness import run_temporal_qa_eval
from virtual_self.eval.temporal_metrics import (
    check_chapter_headers_false_precision,
    check_reply_date_precision,
    check_bundle_validity_expectations,
)
from virtual_self.eval.temporal_scenarios import (
    SUITE_VERSION,
    load_temporal_scenarios,
)
from virtual_self.persistence.db import execute, fetch_one, transaction
from virtual_self.services.memories import create_memory
from virtual_self.services.personality import bootstrap_personality, compile_context
from virtual_self.services.temporal_retrieval import parse_temporal_intent

ROOT = Path(__file__).resolve().parents[1]


def test_temporal_suite_has_twenty_scenarios():
    scenarios = load_temporal_scenarios()
    assert len(scenarios) == 20
    assert SUITE_VERSION == "temporal_qa_v0"


def test_temporal_scenarios_expected_intents_parse():
    failures: list[str] = []
    for sc in load_temporal_scenarios():
        if sc.expected_intent is None:
            continue
        parsed = parse_temporal_intent(sc.prompt)
        if parsed.kind != sc.expected_intent:
            failures.append(f"{sc.id}: expected {sc.expected_intent}, got {parsed.kind}")
    assert not failures, "; ".join(failures)


def test_false_precision_reply_year_only():
    ok = check_reply_date_precision("I think that was in 2021.", max_precision="year")
    assert ok.passed
    bad = check_reply_date_precision("That was on 2021-03-14 for sure.", max_precision="year")
    assert not bad.passed
    assert "false_precision_iso_date" in bad.flags


def test_false_precision_headers():
    groups = [{"header": "2025 · rebuild · year precision"}]
    assert check_chapter_headers_false_precision(groups).passed
    groups_bad = [{"header": "2025-03-14 · rebuild · year precision"}]
    assert not check_chapter_headers_false_precision(groups_bad).passed


def test_run_temporal_eval_dry_run(tmp_path):
    report = run_temporal_qa_eval(dry_run=True, out_dir=tmp_path)
    assert report["dry_run"] is True
    assert report["scenario_count"] == 20
    assert report["reply_count"] == 0
    assert report["metrics"]["intent_failures"] == 0
    assert report["metrics"]["false_precision_failures"] == 0
    path = Path(report["report_path"])
    assert path.exists()


def test_run_eval_cli_temporal_dry_run(tmp_path):
    script = ROOT / "eval" / "run_eval.py"
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--suite",
            "temporal",
            "--dry-run",
            "--out-dir",
            str(tmp_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    reports = list(tmp_path.glob("*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["suite_version"] == "temporal_qa_v0"


def _insert_active_chapter(conn, subject_id: UUID, title: str, *, vf, vt) -> UUID:
    career = fetch_one(conn, "SELECT id FROM life_strands WHERE slug = %s", ("career",))
    chapter_id = uuid7()
    execute(
        conn,
        """
        INSERT INTO life_chapters (
          id, subject_id, strand_id, title, summary, status,
          valid_from, valid_to, time_precision, valid_from_precision, valid_to_precision
        ) VALUES (%s, %s, %s, %s, %s, 'active', %s, %s, 'year', 'year', 'year')
        """,
        (chapter_id, subject_id, career["id"], title, "temporal eval chapter", vf, vt),
    )
    return chapter_id


def test_temporal_eval_now_excludes_closed_validity(ensure_db, auth_headers):
    auth = resolve_auth(auth_headers["Authorization"])
    bootstrap_personality(auth)
    open_needle = "T7_OPEN_VALIDITY_NEEDLE"
    closed_needle = "T7_CLOSED_VALIDITY_NEEDLE"
    chapter_title = "T7_TEMPORAL_EVAL_CHAPTER"

    vf = datetime(2020, 1, 1, tzinfo=timezone.utc)
    with transaction() as conn:
        chapter_id = _insert_active_chapter(
            conn,
            auth.subject_id,
            chapter_title,
            vf=vf,
            vt=None,
        )
        open_mem = create_memory(auth, kind="fact", content=open_needle, scope="private")
        closed_mem = create_memory(auth, kind="fact", content=closed_needle, scope="private")
        for mid in (open_mem["id"], closed_mem["id"]):
            execute(
                conn,
                """
                UPDATE memories SET chapter_id = %s, valid_from = %s, time_precision = 'year'
                WHERE id = %s
                """,
                (chapter_id, vf, UUID(mid)),
            )
        execute(
            conn,
            "UPDATE memories SET valid_to = %s WHERE id = %s",
            (datetime(2022, 1, 1, tzinfo=timezone.utc), UUID(closed_mem["id"])),
        )

    bundle = compile_context(
        auth,
        query=f"these days what matters in {chapter_title}? {open_needle[:8]}",
        memory_limit=8,
    )
    assert bundle["provenance"]["temporal_intent"] in ("now", "chapter")
    check = check_bundle_validity_expectations(
        bundle,
        exclude_content_substrings=(closed_needle,),
        require_content_substrings=(open_needle,),
    )
    assert check.passed, check.flags
    headers = " ".join(g["header"] for g in bundle.get("memory_chapters") or [])
    assert "year precision" in headers
    assert "-03-15" not in headers
