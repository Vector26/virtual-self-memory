"""W-2: SearXNG compose contract (profile web, localhost port, JSON + limiter env)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose.yaml"
SETTINGS = ROOT / "web" / "searxng" / "settings.yml"
SERVICE = "searxng"
CONTAINER = "virtual_self_searxng"


def _load_compose() -> dict:
    with COMPOSE.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_searxng_service_web_profile_localhost_port():
    data = _load_compose()
    svc = data["services"][SERVICE]
    assert "web" in svc.get("profiles", []), "searxng must use profile 'web'"
    ports = svc.get("ports") or []
    assert any(str(p).startswith("127.0.0.1:") for p in ports), "port must bind 127.0.0.1 only"


def test_searxng_on_egress_network_not_internal():
    data = _load_compose()
    svc = data["services"][SERVICE]
    nets = svc.get("networks")
    assert nets == ["web_egress"], "searxng must join web_egress only"
    egress = data["networks"]["web_egress"]
    assert egress.get("internal") is not True


def test_searxng_limiter_and_secret_from_env():
    data = _load_compose()
    env = data["services"][SERVICE].get("environment") or {}
    assert env.get("SEARXNG_LIMITER") == "true"
    assert "SEARXNG_SECRET" in env
    assert "SEARXNG_SECRET_KEY" in str(env.get("SEARXNG_SECRET"))


def test_searxng_settings_json_format_enabled():
    with SETTINGS.open(encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    assert settings.get("use_default_settings") is True
    formats = settings.get("search", {}).get("formats") or []
    assert "json" in formats


def test_env_example_documents_searxng_secret():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "SEARXNG_SECRET_KEY" in example
    assert "SEARXNG_PORT" in example


@pytest.mark.live_searxng
@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_live_searxng_json_search_when_running():
    running = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER],
        capture_output=True,
        text=True,
        check=False,
    )
    if running.returncode != 0 or running.stdout.strip() != "true":
        pytest.skip(f"{CONTAINER} not running")

    port_proc = subprocess.run(
        [
            "docker",
            "port",
            CONTAINER,
            "8080/tcp",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if port_proc.returncode != 0:
        pytest.skip("searxng port not published")
    host_port = port_proc.stdout.strip().split(":")[-1]

    proc = subprocess.run(
        [
            "curl",
            "-sS",
            "-m",
            "20",
            f"http://127.0.0.1:{host_port}/search?q=test&format=json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip("searxng search request failed (engines may be unreachable)")
    payload = json.loads(proc.stdout)
    results = payload.get("results") or []
    assert len(results) >= 1
