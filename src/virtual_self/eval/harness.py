from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from virtual_self.eval.heldout_baseline import heldout_stylometry_baselines
from virtual_self.eval.scenarios import SUITE_VERSION, EvalScenario, load_scenarios
from virtual_self.eval.slop import check_slop
from virtual_self.eval.stylometry import stylometry_features
from virtual_self.eval.temporal_metrics import (
    check_reply_date_precision,
)
from virtual_self.eval.temporal_scenarios import (
    SUITE_VERSION as TEMPORAL_SUITE_VERSION,
    TemporalQAScenario,
    load_temporal_scenarios,
)
from virtual_self.eval.trait_interview_scenarios import (
    SUITE_VERSION as TRAIT_MODE_SUITE_VERSION,
    TraitInterviewScenario,
    load_trait_interview_scenarios,
)
from virtual_self.eval.mode_adherence_scenarios import (
    ModeAdherenceScenario,
    load_mode_adherence_scenarios,
)
from virtual_self.eval.trait_fidelity_metrics import (
    assert_report_has_no_raw_scores,
    interview_fidelity_vs_ipip,
    summarize_trait_fidelity,
)
from virtual_self.eval.mode_adherence_metrics import (
    check_council_format,
    check_no_mode_bleed,
    check_requested_mode_adherence,
    summarize_mode_adherence,
    ModeAdherenceCheck,
)
from virtual_self.eval.consistency_scenarios import (
    SUITE_VERSION as CONSISTENCY_SUITE_VERSION,
    ConsistencyScenario,
    load_consistency_scenarios,
)
from virtual_self.eval.consistency_metrics import (
    check_turn_consistency,
    summarize_consistency,
)
from virtual_self.services.temporal_retrieval import parse_temporal_intent

_LOCALHOST = re.compile(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?/", re.I)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def persona_eval_state_dir() -> Path:
    return _project_root() / ".state" / "persona_eval"


def temporal_qa_state_dir() -> Path:
    return _project_root() / ".state" / "temporal_qa"


def trait_mode_eval_state_dir() -> Path:
    return _project_root() / ".state" / "trait_mode_eval"


def consistency_eval_state_dir() -> Path:
    return _project_root() / ".state" / "consistency_eval"


def _assert_localhost_base(base_url: str) -> None:
    if not _LOCALHOST.match(base_url.rstrip("/") + "/"):
        raise ValueError(f"eval harness v0 allows localhost only; got {base_url}")


def _chat_turn(
    base_url: str,
    token: str,
    query: str,
    *,
    history: list[dict[str, str]] | None = None,
) -> str | None:
    _assert_localhost_base(base_url)
    url = f"{base_url.rstrip('/')}/v1/chat:turn"
    body: dict[str, Any] = {"query": query}
    if history:
        body["history"] = history
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"chat API HTTP {exc.code}: {detail}") from exc
    reply = body.get("reply")
    return str(reply).strip() if reply else None


def _scenario_result(scenario: EvalScenario, reply: str | None) -> dict[str, Any]:
    features_dict: dict[str, Any] | None = None
    slop_dict: dict[str, Any] | None = None
    if reply is not None:
        slop_dict = check_slop(
            reply,
            register=scenario.register,
            format_requested=scenario.format_requested,
        ).to_dict()
        features_dict = stylometry_features(reply).to_dict()
    return {
        **scenario.to_dict(),
        "reply": reply,
        "slop": slop_dict,
        "features": features_dict,
    }


def run_persona_eval(
    *,
    dry_run: bool = False,
    base_url: str = "http://127.0.0.1:8090",
    token: str | None = None,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    scenarios = load_scenarios()
    heldout = heldout_stylometry_baselines()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    results: list[dict[str, Any]] = []
    replies_with_slop = 0
    reply_count = 0

    for scenario in scenarios:
        reply: str | None = None
        if not dry_run:
            if not token:
                raise ValueError("token required when not dry-run")
            reply = _chat_turn(base_url, token, scenario.prompt)
        row = _scenario_result(scenario, reply)
        results.append(row)
        if reply is not None:
            reply_count += 1
            if row["slop"] and not row["slop"]["passed"]:
                replies_with_slop += 1

    slop_rate = (replies_with_slop / reply_count) if reply_count else None
    report: dict[str, Any] = {
        "run_id": run_id,
        "suite_version": SUITE_VERSION,
        "dry_run": dry_run,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "scenario_count": len(scenarios),
        "reply_count": reply_count,
        "heldout_baseline": heldout,
        "metrics": {
            "slop_rate": slop_rate,
            "replies_with_slop": replies_with_slop,
        },
        "scenarios": results,
    }

    target = out_dir or persona_eval_state_dir()
    target.mkdir(parents=True, exist_ok=True)
    out_path = target / f"{run_id}.json"
    out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    report["report_path"] = str(out_path)
    return report


def _temporal_scenario_result(
    scenario: TemporalQAScenario,
    reply: str | None,
) -> dict[str, Any]:
    parsed = parse_temporal_intent(scenario.prompt)
    intent_ok = (
        scenario.expected_intent is None or parsed.kind == scenario.expected_intent
    )
    intent_check = {
        "parsed_kind": parsed.kind,
        "expected_kind": scenario.expected_intent,
        "passed": intent_ok,
    }
    precision_dict: dict[str, Any] | None = None
    if reply is not None:
        precision_dict = check_reply_date_precision(
            reply,
            max_precision=scenario.reply_max_precision,
        ).to_dict()
    passed = intent_ok and (precision_dict is None or precision_dict.get("passed", True))
    return {
        **scenario.to_dict(),
        "reply": reply,
        "intent_check": intent_check,
        "false_precision": precision_dict,
        "passed": passed,
    }


def run_temporal_qa_eval(
    *,
    dry_run: bool = False,
    base_url: str = "http://127.0.0.1:8090",
    token: str | None = None,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    scenarios = load_temporal_scenarios()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    results: list[dict[str, Any]] = []
    reply_count = 0
    false_precision_failures = 0
    intent_failures = 0

    for scenario in scenarios:
        reply: str | None = None
        if not dry_run:
            if not token:
                raise ValueError("token required when not dry-run")
            reply = _chat_turn(base_url, token, scenario.prompt)
        row = _temporal_scenario_result(scenario, reply)
        results.append(row)
        if not row["intent_check"]["passed"]:
            intent_failures += 1
        if reply is not None:
            reply_count += 1
            if row["false_precision"] and not row["false_precision"]["passed"]:
                false_precision_failures += 1

    false_precision_rate = (
        false_precision_failures / reply_count if reply_count else None
    )
    report: dict[str, Any] = {
        "run_id": run_id,
        "suite_version": TEMPORAL_SUITE_VERSION,
        "dry_run": dry_run,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "scenario_count": len(scenarios),
        "reply_count": reply_count,
        "metrics": {
            "intent_failures": intent_failures,
            "false_precision_failures": false_precision_failures,
            "false_precision_rate": false_precision_rate,
        },
        "scenarios": results,
    }

    target = out_dir or temporal_qa_state_dir()
    target.mkdir(parents=True, exist_ok=True)
    out_path = target / f"{run_id}.json"
    out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    report["report_path"] = str(out_path)
    return report


def _trait_interview_row(scenario: TraitInterviewScenario, reply: str | None) -> dict[str, Any]:
    inferred = None
    if reply:
        from virtual_self.eval.trait_fidelity_metrics import infer_trait_band_from_reply

        inferred = infer_trait_band_from_reply(scenario.primary_trait, reply)
    return {
        **scenario.to_dict(),
        "reply": reply,
        "inferred_band": inferred,
    }


def _mode_adherence_row(
    scenario: ModeAdherenceScenario,
    reply: str | None,
    *,
    gate_blocked: bool,
) -> dict[str, Any]:
    check: ModeAdherenceCheck | None = None
    if reply is not None and not gate_blocked:
        if scenario.kind == "requested_mode" and scenario.expected_mode:
            check = check_requested_mode_adherence(reply, scenario.expected_mode)
        elif scenario.kind == "council":
            check = check_council_format(reply)
        elif scenario.kind == "no_mode_bleed":
            check = check_no_mode_bleed(
                reply,
                allow_mode_names=scenario.allow_mode_names,
            )
        if check is not None:
            check = ModeAdherenceCheck(
                scenario_id=scenario.id,
                kind=check.kind,
                passed=check.passed,
                expected_mode=check.expected_mode,
                flags=check.flags,
            )
    return {
        **scenario.to_dict(),
        "reply": reply,
        "check": check.to_dict() if check else None,
        "skipped_gate": gate_blocked and scenario.kind != "no_mode_bleed",
    }


def run_trait_mode_eval(
    *,
    dry_run: bool = False,
    base_url: str = "http://127.0.0.1:8090",
    token: str | None = None,
    out_dir: Path | None = None,
    ipip_path: Path | None = None,
) -> dict[str, Any]:
    trait_scenarios = load_trait_interview_scenarios()
    mode_scenarios = load_mode_adherence_scenarios()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    trait_results: list[dict[str, Any]] = []
    mode_results: list[dict[str, Any]] = []
    replies_by_trait: dict[str, list[str]] = {}
    reply_count = 0
    mode_gate_blocked = False
    mode_gate_reason: str | None = None

    for scenario in trait_scenarios:
        reply: str | None = None
        if not dry_run:
            if not token:
                raise ValueError("token required when not dry-run")
            reply = _chat_turn(base_url, token, scenario.prompt)
        row = _trait_interview_row(scenario, reply)
        trait_results.append(row)
        if reply:
            reply_count += 1
            replies_by_trait.setdefault(scenario.primary_trait, []).append(reply)

    for scenario in mode_scenarios:
        reply = None
        skip_live = mode_gate_blocked and scenario.kind in ("requested_mode", "council")
        if not dry_run and not skip_live:
            if not token:
                raise ValueError("token required when not dry-run")
            try:
                reply = _chat_turn(base_url, token, scenario.prompt)
            except RuntimeError as exc:
                msg = str(exc)
                if scenario.kind in ("requested_mode", "council") and (
                    "approved" in msg.lower() or "404" in msg
                ):
                    mode_gate_blocked = True
                    mode_gate_reason = (
                        "mode versions still draft — approve TR-4 cards before live mode adherence"
                    )
                    skip_live = True
                else:
                    raise
        row = _mode_adherence_row(
            scenario,
            reply,
            gate_blocked=skip_live and scenario.kind in ("requested_mode", "council"),
        )
        mode_results.append(row)
        if reply:
            reply_count += 1

    trait_checks = interview_fidelity_vs_ipip(replies_by_trait, ipip_path=ipip_path)
    trait_summary = summarize_trait_fidelity(trait_checks)

    mode_checks: list[ModeAdherenceCheck] = []
    for row in mode_results:
        raw = row.get("check")
        if not raw:
            continue
        mode_checks.append(
            ModeAdherenceCheck(
                scenario_id=row["id"],
                kind=raw["kind"],
                passed=raw["passed"],
                expected_mode=raw.get("expected_mode"),
                flags=tuple(raw.get("flags") or ()),
            )
        )
    mode_summary = summarize_mode_adherence(mode_checks)

    report: dict[str, Any] = {
        "run_id": run_id,
        "suite_version": TRAIT_MODE_SUITE_VERSION,
        "dry_run": dry_run,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "scenario_count": len(trait_scenarios) + len(mode_scenarios),
        "reply_count": reply_count,
        "mode_approval_gate": {
            "blocked": mode_gate_blocked,
            "reason": mode_gate_reason,
            "note": (
                "Live requested-mode/council metrics require persona_mode_versions.status=active. "
                "Tests seed approved versions; see docs/TR7_EVAL_GATE.md."
            ),
        },
        "metrics": {
            "trait_fidelity": trait_summary,
            "mode_adherence": mode_summary,
        },
        "trait_interview": trait_results,
        "mode_adherence": mode_results,
    }

    target = out_dir or trait_mode_eval_state_dir()
    target.mkdir(parents=True, exist_ok=True)
    out_path = target / f"{run_id}.json"
    body = json.dumps(report, indent=2) + "\n"
    assert_report_has_no_raw_scores(body)
    out_path.write_text(body, encoding="utf-8")
    report["report_path"] = str(out_path)
    return report


def _consistency_row(
    scenario: ConsistencyScenario,
    reply: str | None,
) -> dict[str, Any]:
    checks_dict: dict[str, Any] | None = None
    passed_all = True
    if reply is not None:
        checks = check_turn_consistency(
            reply,
            prompt=scenario.prompt,
            memory_bundle=scenario.memory_bundle,
            history=scenario.history,
        )
        checks_dict = {k: v.to_dict() for k, v in checks.items()}
        passed_all = all(v.passed for v in checks.values())
    return {
        **scenario.to_dict(),
        "reply": reply,
        "consistency": checks_dict,
        "passed_all_checks": passed_all if reply is not None else None,
    }


def run_consistency_eval(
    *,
    dry_run: bool = False,
    base_url: str = "http://127.0.0.1:8090",
    token: str | None = None,
    out_dir: Path | None = None,
    use_fixtures: bool = True,
) -> dict[str, Any]:
    scenarios = load_consistency_scenarios()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    results: list[dict[str, Any]] = []
    reply_count = 0

    for scenario in scenarios:
        reply: str | None = None
        if dry_run or use_fixtures:
            reply = scenario.fixture_reply
        elif not dry_run:
            if not token:
                raise ValueError("token required when not dry-run")
            history = [{"role": r, "content": c} for r, c in scenario.history]
            reply = _chat_turn(
                base_url,
                token,
                scenario.prompt,
                history=history or None,
            )
        row = _consistency_row(scenario, reply)
        results.append(row)
        if reply is not None:
            reply_count += 1

    summary = summarize_consistency(results)
    report: dict[str, Any] = {
        "run_id": run_id,
        "suite_version": CONSISTENCY_SUITE_VERSION,
        "dry_run": dry_run,
        "use_fixtures": use_fixtures,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "scenario_count": len(scenarios),
        "reply_count": reply_count,
        "metrics": {
            "consistency": summary,
        },
        "scenarios": results,
    }

    target = out_dir or consistency_eval_state_dir()
    target.mkdir(parents=True, exist_ok=True)
    out_path = target / f"{run_id}.json"
    out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    report["report_path"] = str(out_path)
    return report
