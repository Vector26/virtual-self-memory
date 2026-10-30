#!/usr/bin/env python3
"""Persona eval harness v0 — scenario runner + deterministic metrics."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from virtual_self.eval.harness import (  # noqa: E402
    run_consistency_eval,
    run_persona_eval,
    run_temporal_qa_eval,
    run_trait_mode_eval,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run persona / temporal QA eval suites")
    parser.add_argument(
        "--suite",
        choices=("persona", "temporal", "trait_mode", "consistency"),
        default="persona",
        help="Eval suite to run (default: persona)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Skip live chat API calls")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("VSM_EVAL_BASE_URL", "http://127.0.0.1:8090"),
        help="Virtual Self API base (localhost only)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("VSM_BOOTSTRAP_TOKEN") or os.environ.get("VSM_EVAL_TOKEN"),
        help="Bearer token for /v1/chat:turn",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Override .state/persona_eval output directory",
    )
    args = parser.parse_args()

    if args.suite == "persona":
        runner = run_persona_eval
    elif args.suite == "temporal":
        runner = run_temporal_qa_eval
    elif args.suite == "trait_mode":
        runner = run_trait_mode_eval
    else:
        runner = run_consistency_eval
    report = runner(
        dry_run=args.dry_run,
        base_url=args.base_url,
        token=args.token,
        out_dir=args.out_dir,
    )
    print(report["report_path"])
    extra = ""
    if args.suite == "temporal":
        m = report.get("metrics") or {}
        extra = (
            f" intent_failures={m.get('intent_failures')} "
            f"false_precision_failures={m.get('false_precision_failures')}"
        )
    elif args.suite == "trait_mode":
        m = report.get("metrics") or {}
        tf = m.get("trait_fidelity") or {}
        ma = m.get("mode_adherence") or {}
        gate = report.get("mode_approval_gate") or {}
        extra = (
            f" traits_within={tf.get('traits_within_tolerance')}/{tf.get('traits_compared')} "
            f" mode_pass_rate={ma.get('requested_mode_pass_rate')} "
            f" bleed_failures={ma.get('mode_bleed_failures')} "
            f" mode_gate_blocked={gate.get('blocked')}"
        )
    elif args.suite == "consistency":
        m = (report.get("metrics") or {}).get("consistency") or {}
        extra = (
            f" all_pass={m.get('turns_all_checks_passed')}/{m.get('scenario_count')} "
            f" ptl_rate={m.get('prompt_to_line_pass_rate')} "
            f" l2l_rate={m.get('line_to_line_pass_rate')} "
            f" qa_rate={m.get('qa_pass_rate')}"
        )
    print(
        f"suite={report['suite_version']} scenarios={report['scenario_count']} "
        f"replies={report['reply_count']} dry_run={report['dry_run']}{extra}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
