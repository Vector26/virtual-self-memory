"""W-4: live adversarial probes against knowledge-gateway (plan §14.6)."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTAINER = "virtual_self_knowledge_gateway"
_SUITE_PATH = ROOT / "scripts" / "web_attack_suite.py"


def _load_suite():
    spec = importlib.util.spec_from_file_location("web_attack_suite", _SUITE_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _gateway_running() -> bool:
    proc = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
async def test_knowledge_gateway_attack_suite_rejects_probes():
    suite = _load_suite()
    if not _gateway_running():
        pytest.skip(f"{CONTAINER} not running")
    token = suite.load_gateway_token()
    if not token:
        pytest.skip("WEB_GATEWAY_MCP_TOKEN missing")

    base = suite.gateway_base_url()
    assert base
    client = suite.GatewayClient(base, token, pause_s=1.25)
    results = await suite.run_cases(client, skip_live=False)

    failures = [r for r in results if r.outcome == "FAIL"]
    assert not failures, "\n".join(
        f"{r.case_id}: {r.description}\n  {r.relay_response[:300]}" for r in failures
    )


def test_web_attack_suite_script_importable():
    suite = _load_suite()
    assert suite.DEFAULT_JSON.name == "web_attack_results.json"
    assert suite.DEFAULT_MD.name == "WEB_ATTACK_RESULTS.md"
