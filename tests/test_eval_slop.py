from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from virtual_self.eval.slop import check_slop
from virtual_self.eval.stylometry import stylometry_features

ROOT = Path(__file__).resolve().parents[1]


def test_slop_banned_closer():
    r = check_slop("Sure thing. Does this match your experience?", register="casual_chat")
    assert not r.passed
    assert any("banned_closer" in f for f in r.flags)


def test_slop_third_person():
    r = check_slop("The user might want to rest.", register="journal")
    assert not r.passed
    assert "third_person_the_user" in r.flags


def test_slop_headers_when_unrequested():
    r = check_slop("## Summary\n\nSome text here.", register="technical")
    assert not r.passed
    assert "markdown_headers_unrequested" in r.flags


def test_slop_allows_headers_when_requested():
    r = check_slop("## Summary\n\n| a | b |\n|---|---|", register="technical", format_requested=True)
    assert "markdown_headers_unrequested" not in r.flags


def test_slop_length_over_norm():
    long_text = "word " * 200
    r = check_slop(long_text, register="casual_chat")
    assert not r.passed
    assert any(f.startswith("length_over_2x_norm") for f in r.flags)


def test_stylometry_basic():
    f = stylometry_features("One sentence. Two sentences?\n")
    assert f.words == 4
    assert f.sentences >= 2
    assert f.question_closer_rate == 1.0


def test_run_eval_dry_run_report(tmp_path):
    script = ROOT / "eval" / "run_eval.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--dry-run", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    reports = list(tmp_path.glob("*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["dry_run"] is True
    assert report["reply_count"] == 0
    assert len(report["scenarios"]) >= 20
    for row in report["scenarios"]:
        assert row["reply"] is None
        assert "slop" in row and row["slop"] is None
        assert "features" in row and row["features"] is None
    assert report["heldout_baseline"]
