from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from virtual_self.archive.detect import classify_file
from virtual_self.archive.llm.supervisor import (
    SUPERVISED_PLAN_SCHEMA_VERSION,
    collect_detected,
    input_fingerprint,
    load_supervised_plan,
    merge_llm_supervisor_output,
    run_supervisor,
    specialist_id_for_product,
    supervised_plan_path,
)
from virtual_self.archive.llm.supervisor_prompts import SUPERVISOR_PROMPT_VERSION


@pytest.fixture
def takeout_tree(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "staging" / "abc123"
    root.mkdir(parents=True)
    chrome = root / "Takeout" / "Chrome" / "Default"
    chrome.mkdir(parents=True)
    (chrome / "History.json").write_text('{"Browser History": []}', encoding="utf-8")
    activity = root / "Takeout" / "My Activity" / "Search"
    activity.mkdir(parents=True)
    (activity / "MyActivity.json").write_text("[]", encoding="utf-8")
    (root / "Takeout" / "random.bin").write_bytes(b"\x00\x01")
    return root, "google"


def test_specialist_id_known_products():
    assert specialist_id_for_product("chrome") == "chrome"
    assert specialist_id_for_product("instagram") is None


def test_collect_detected_fixture(takeout_tree):
    root, provider = takeout_tree
    detected = collect_detected(root, provider)
    products = {d.product for d in detected}
    assert "chrome" in products
    assert "my_activity" in products
    assert "unknown" in products


def test_heuristic_abstains_unknown(takeout_tree):
    root, provider = takeout_tree
    detected = collect_detected(root, provider)
    unknown = [d for d in detected if d.product == "unknown"][0]
    with patch("virtual_self.archive.llm.supervisor.get_settings") as gs:
        gs.return_value.archive_llm_enabled = False
        plan = run_supervisor(root, provider, archive_run_id=None, persist=False)
    unk_assign = [a for a in plan["assignments"] if unknown.rel_path in a["rel_paths"]][0]
    assert unk_assign["abstain"] is True
    assert unk_assign["skip"] is True
    assert unk_assign["specialist_id"] is None


def test_merge_llm_assignments_cite_detect_keys(takeout_tree):
    root, provider = takeout_tree
    detected = collect_detected(root, provider)
    chrome = next(d for d in detected if d.product == "chrome")

    def fake_llm(_system: str, user: str, **_kwargs):
        payload = json.loads(user)
        assert payload["files"]
        return {
            "assignments": [
                {
                    "kind": "file",
                    "rel_paths": [chrome.rel_path],
                    "detect_cite": {"provider": "google", "product": "chrome"},
                    "specialist_id": "chrome",
                    "priority": 7,
                    "sensitivity_hint": 2,
                    "skip": False,
                    "abstain": False,
                }
            ]
        }

    with patch("virtual_self.archive.llm.supervisor.get_settings") as gs:
        gs.return_value.archive_llm_enabled = True
        plan = run_supervisor(
            root,
            provider,
            archive_run_id=None,
            chat_json_fn=fake_llm,
            persist=False,
        )
    assert plan["prompt_version"] == SUPERVISOR_PROMPT_VERSION
    assert plan["schema_version"] == SUPERVISED_PLAN_SCHEMA_VERSION
    chrome_a = [a for a in plan["assignments"] if chrome.rel_path in a["rel_paths"]][0]
    assert chrome_a["detect_cite"]["product"] == "chrome"
    assert chrome_a["specialist_id"] == "chrome"


def test_idempotent_plan_file(takeout_tree):
    root, provider = takeout_tree
    detected = collect_detected(root, provider)
    fp = input_fingerprint(provider, detected)

    with patch("virtual_self.archive.llm.supervisor.get_settings") as gs:
        gs.return_value.archive_llm_enabled = False
        first = run_supervisor(root, provider, archive_run_id=None, persist=True)
        assert supervised_plan_path(root).is_file()

        call_count = 0

        def boom(*_a, **_k):
            nonlocal call_count
            call_count += 1
            return {"assignments": []}

        gs.return_value.archive_llm_enabled = True
        second = run_supervisor(
            root,
            provider,
            archive_run_id=None,
            chat_json_fn=boom,
            persist=True,
        )
    assert call_count == 0
    assert second["input_fingerprint"] == fp
    assert second == first
    cached = load_supervised_plan(root)
    assert cached is not None
    assert cached["input_fingerprint"] == fp


def test_batch_file_paths_subset(takeout_tree):
    root, provider = takeout_tree
    chrome_path = root / "Takeout" / "Chrome" / "Default" / "History.json"
    detected = collect_detected(root, provider, file_paths=[chrome_path])
    assert len(detected) == 1
    assert detected[0].product == "chrome"
    d = classify_file(root, chrome_path, default_provider=provider)
    assert d.product == "chrome"


def test_invalid_llm_product_abstains_to_heuristic(takeout_tree):
    root, provider = takeout_tree
    detected = collect_detected(root, provider)

    def bad_llm(_s, _u, _t):
        return {
            "assignments": [
                {
                    "kind": "file",
                    "rel_paths": [detected[0].rel_path],
                    "detect_cite": {"provider": "google", "product": "not_a_real_product"},
                    "specialist_id": "chrome",
                    "priority": 5,
                    "sensitivity_hint": 1,
                    "skip": False,
                    "abstain": False,
                }
            ]
        }

    plan = merge_llm_supervisor_output(bad_llm("", "", 0.0), root, provider, detected)
    # Invalid cite dropped; files still covered via fallback per file
    assert len(plan["assignments"]) >= len(detected)
