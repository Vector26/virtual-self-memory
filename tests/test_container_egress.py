"""W-1: vself containers must not have WAN egress (compose contract + optional Docker probes)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose.yaml"
DOC = ROOT / "docs" / "WEB_EGRESS.md"
NETWORK_NAME = "virtual_self_internal"


def _load_compose() -> dict:
    with COMPOSE.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_compose_internal_network_blocks_wan_egress():
    data = _load_compose()
    internal_net = data["networks"]["internal"]
    assert internal_net.get("name") == NETWORK_NAME
    assert internal_net.get("internal") is True


def test_vsm_services_use_internal_network_only():
    data = _load_compose()
    net_names = set(data["networks"])
    # Web stack (SearXNG, later gateway) uses web_egress — not the vself internal LAN.
    web_stack = {"searxng", "knowledge-gateway"}
    for name, svc in data["services"].items():
        if name in web_stack:
            nets = svc.get("networks")
            if name == "knowledge-gateway":
                assert nets == ["internal", "web_egress"], (
                    f"{name} must join internal + web_egress"
                )
            else:
                assert nets == ["web_egress"], f"{name} must use web_egress only"
            continue
        if name == "postgres":
            continue
        nets = svc.get("networks")
        if nets is None:
            continue
        if isinstance(nets, list):
            assert nets == ["internal"], f"{name} must only join internal network"
        else:
            assert set(nets) <= net_names


def test_host_docker_internal_extra_hosts_on_app_services():
    data = _load_compose()
    anchor = data["x-vsm-common"]
    assert any("host.docker.internal" in h for h in anchor["extra_hosts"])
    for key in ("VSM_OLLAMA_URL", "VSM_OPENAI_BASE_URL", "VSM_NOTIFIER_URL"):
        assert "host.docker.internal" in anchor["environment"][key]


def test_web_egress_doc_exists_and_describes_mechanism():
    text = DOC.read_text(encoding="utf-8")
    assert "internal: true" in text
    assert NETWORK_NAME in text
    assert "host.docker.internal" in text


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_live_network_is_internal_when_present():
    proc = subprocess.run(
        ["docker", "network", "inspect", NETWORK_NAME, "--format", "{{json .Internal}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip("virtual_self_internal network not present")
    assert json.loads(proc.stdout.strip()) is True


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_live_api_container_has_no_public_https_egress():
    running = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", "virtual_self_api"],
        capture_output=True,
        text=True,
        check=False,
    )
    if running.returncode != 0 or running.stdout.strip() != "true":
        pytest.skip("virtual_self_api not running")

    proc = subprocess.run(
        [
            "docker",
            "exec",
            "virtual_self_api",
            "sh",
            "-c",
            "curl -sS -m 5 -o /dev/null -w '%{http_code}' https://1.1.1.1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    # With internal network, curl should fail (nonzero) or not return HTTP success.
    assert proc.returncode != 0 or proc.stdout.strip() not in ("200", "301", "302")


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_live_api_reaches_host_llm_port():
    running = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", "virtual_self_api"],
        capture_output=True,
        text=True,
        check=False,
    )
    if running.returncode != 0 or running.stdout.strip() != "true":
        pytest.skip("virtual_self_api not running")

    proc = subprocess.run(
        [
            "docker",
            "exec",
            "virtual_self_api",
            "sh",
            "-c",
            "curl -sS -m 3 -o /dev/null -w '%{http_code}' http://host.docker.internal:8081/v1/models",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip("host LLM not reachable on :8081 from container")
    assert proc.stdout.strip() == "200"
