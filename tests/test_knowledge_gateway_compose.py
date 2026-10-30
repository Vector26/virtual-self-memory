"""W-3: mcp-searxng-relay (knowledge-gateway) compose contract + optional live MCP probes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose.yaml"
SERVICE = "knowledge-gateway"
CONTAINER = "virtual_self_knowledge_gateway"


def _load_compose() -> dict:
    with COMPOSE.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_knowledge_gateway_web_profile_localhost_port():
    data = _load_compose()
    svc = data["services"][SERVICE]
    assert "web" in svc.get("profiles", []), "knowledge-gateway must use profile 'web'"
    ports = svc.get("ports") or []
    assert any(str(p).startswith("127.0.0.1:") for p in ports), "port must bind 127.0.0.1 only"


def test_knowledge_gateway_dual_network_and_hardening():
    data = _load_compose()
    svc = data["services"][SERVICE]
    assert svc.get("networks") == ["internal", "web_egress"]
    assert svc.get("read_only") is True
    assert svc.get("mem_limit") == "256m"
    env = svc.get("environment") or {}
    assert env.get("MAX_IMAGE_BYTES") == "0"
    assert env.get("MAX_OFFICE_BYTES") == "0"
    assert "WEB_GATEWAY_MCP_TOKEN" in str(env.get("MCP_AUTH_TOKEN"))
    assert env.get("SEARXNG_URL") == "http://searxng:8080"


def test_knowledge_gateway_pinned_relay_image():
    data = _load_compose()
    image = data["services"][SERVICE].get("image") or ""
    assert "littleoffice/mcp-searxng-relay" in image
    assert "@sha256:" in image or "sha256-" in image


def test_env_example_documents_web_gateway_token():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "WEB_GATEWAY_MCP_TOKEN" in example
    assert "WEB_GATEWAY_PORT" in example


def _gateway_base_url() -> str | None:
    running = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER],
        capture_output=True,
        text=True,
        check=False,
    )
    if running.returncode != 0 or running.stdout.strip() != "true":
        return None
    port_proc = subprocess.run(
        ["docker", "port", CONTAINER, "8080/tcp"],
        capture_output=True,
        text=True,
        check=False,
    )
    if port_proc.returncode != 0:
        return None
    host_port = port_proc.stdout.strip().split(":")[-1]
    return f"http://127.0.0.1:{host_port}"


def _gateway_token() -> str | None:
    token = os.environ.get("WEB_GATEWAY_MCP_TOKEN", "").strip()
    if len(token) >= 32:
        return token
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("WEB_GATEWAY_MCP_TOKEN="):
            val = line.split("=", 1)[1].strip().strip('"').strip("'")
            return val if len(val) >= 32 else None
    return None


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_live_knowledge_gateway_health_when_running():
    base = _gateway_base_url()
    if not base:
        pytest.skip(f"{CONTAINER} not running")
    proc = subprocess.run(
        ["curl", "-sS", "-m", "5", "-o", "/dev/null", "-w", "%{http_code}", f"{base}/health"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "200"


@pytest.mark.asyncio
@pytest.mark.live_searxng
@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
async def test_live_mcp_search_and_read_when_running():
    base = _gateway_base_url()
    token = _gateway_token()
    if not base or not token:
        pytest.skip("knowledge-gateway not running or WEB_GATEWAY_MCP_TOKEN missing")

    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(headers=headers, timeout=60.0) as http_client:
        async with streamable_http_client(f"{base}/", http_client=http_client) as (
            read_stream,
            write_stream,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                search = await session.call_tool(
                    "searxng_web_search",
                    {"query": "searxng metasearch", "num_results": 3},
                )
                assert not search.is_error
                search_text = "".join(
                    block.text for block in search.content if hasattr(block, "text")
                )
                assert "searxng" in search_text.lower() or len(search_text) > 40

                read = await session.call_tool(
                    "searxng_read_url",
                    {"url": "https://example.com/"},
                )
                assert not read.is_error
                read_text = "".join(
                    block.text for block in read.content if hasattr(block, "text")
                )
                assert "example" in read_text.lower()


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_live_compose_config_includes_gateway():
    proc = subprocess.run(
        ["docker", "compose", "--profile", "web", "-f", str(COMPOSE), "config"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "WEB_GATEWAY_MCP_TOKEN": "x" * 32, "SEARXNG_SECRET_KEY": "y" * 32},
    )
    assert proc.returncode == 0, proc.stderr
    assert "virtual_self_knowledge_gateway" in proc.stdout
