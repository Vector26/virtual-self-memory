"""G-2: prompt-to-line, line-to-line, and Q&A consistency metrics."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from virtual_self.eval.consistency_metrics import (
    check_line_to_line_consistency,
    check_prompt_to_line_consistency,
    check_qa_consistency,
    check_turn_consistency,
    extract_polarity_claims,
    flatten_lineage_text,
)
from virtual_self.eval.consistency_scenarios import (
    SUITE_VERSION,
    load_consistency_scenarios,
)
from virtual_self.eval.harness import run_consistency_eval

ROOT = Path(__file__).resolve().parents[1]


def test_suite_version_and_scenario_expectations():
    scenarios = load_consistency_scenarios()
    assert SUITE_VERSION == "consistency_eval_v0"
    assert len(scenarios) == 12
    for sc in scenarios:
        checks = check_turn_consistency(
            sc.fixture_reply,
            prompt=sc.prompt,
            memory_bundle=sc.memory_bundle,
            history=sc.history,
        )
        all_pass = all(c.passed for c in checks.values())
        assert all_pass == sc.expect_all_pass, sc.id


def test_prompt_to_line_detects_bundle_contradiction():
    bundle = {"relevant_memories": [{"content": "I prefer minimal tools."}]}
    ok = check_prompt_to_line_consistency(
        "You prefer minimal tools for solo work.",
        prompt="tools?",
        memory_bundle=bundle,
    )
    assert ok.passed
    bad = check_prompt_to_line_consistency(
        "You hate minimal tools.",
        prompt="tools?",
        memory_bundle=bundle,
    )
    assert not bad.passed
    assert any("minimal tools" in f for f in bad.flags)


def test_line_to_line_adjacent_contradiction():
    ok = check_line_to_line_consistency("You love remote work.")
    assert ok.passed
    bad = check_line_to_line_consistency(
        "You love remote work. You hate remote work."
    )
    assert not bad.passed


def test_qa_contradicts_prior_assistant():
    history = [
        ("user", "burnout?"),
        ("assistant", "You were burned out and needed rest."),
    ]
    ok = check_qa_consistency("You are still burned out.", history)
    assert ok.passed
    bad = check_qa_consistency(
        "You were never burned out — you love grinding every night.",
        history,
    )
    assert not bad.passed


def test_flatten_lineage_includes_prompt_and_memories():
    text = flatten_lineage_text(
        "my question",
        {
            "relevant_memories": [{"content": "memory line"}],
            "behavioral_constraints": ["be honest"],
        },
    )
    assert "my question" in text
    assert "memory line" in text
    assert "be honest" in text


def test_extract_polarity_claims_multiple_affect_verbs():
    claims = extract_polarity_claims("I prefer remote and hate local.")
    keys = {c.key for c in claims}
    assert "remote" in keys
    assert "local" in keys
    assert len(claims) >= 2


def test_run_consistency_eval_dry_run(tmp_path: Path):
    report = run_consistency_eval(dry_run=True, out_dir=tmp_path)
    assert report["dry_run"] is True
    assert report["scenario_count"] == 12
    assert report["reply_count"] == 12
    metrics = report["metrics"]["consistency"]
    assert metrics["scenario_count"] == 12
    assert metrics["turns_all_checks_passed"] == sum(
        1 for s in report["scenarios"] if s["passed_all_checks"]
    )
    assert "prompt_to_line_pass_rate" in metrics
    path = Path(report["report_path"])
    assert path.exists()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["metrics"]["consistency"]["failures_by_kind"]["prompt_to_line"] >= 1


def test_run_eval_cli_consistency_dry_run(tmp_path: Path):
    script = ROOT / "eval" / "run_eval.py"
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--suite",
            "consistency",
            "--dry-run",
            "--out-dir",
            str(tmp_path),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "consistency_eval_v0" in proc.stdout
