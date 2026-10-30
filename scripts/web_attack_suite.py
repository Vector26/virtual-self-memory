#!/usr/bin/env python3
"""Run plan §14.6 adversarial probes against the knowledge-gateway (mcp-searxng-relay)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

ROOT = Path(__file__).resolve().parents[1]
CONTAINER = "virtual_self_knowledge_gateway"
DEFAULT_JSON = ROOT / ".state" / "web_attack_results.json"
DEFAULT_MD = ROOT / "docs" / "WEB_ATTACK_RESULTS.md"

Outcome = Literal["PASS", "FAIL", "SKIP"]
ProbeKind = Literal[
    "ssrf",
    "auth",
    "scheme",
    "resource",
    "injection",
    "scope",
    "leak",
    "method",
]


@dataclass
class CaseResult:
    case_id: str
    category: ProbeKind
    description: str
    outcome: Outcome
    relay_response: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SuiteReport:
    generated_at: str
    gateway_base: str
    gateway_running: bool
    results: list[CaseResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "gateway_base": self.gateway_base,
            "gateway_running": self.gateway_running,
            "results": [r.to_dict() for r in self.results],
            "summary": {
                "pass": sum(1 for r in self.results if r.outcome == "PASS"),
                "fail": sum(1 for r in self.results if r.outcome == "FAIL"),
                "skip": sum(1 for r in self.results if r.outcome == "SKIP"),
            },
        }


def load_gateway_token() -> str | None:
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


def gateway_base_url() -> str | None:
    running = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER],
        capture_output=True,
        text=True,
        check=False,
    )
    if running.returncode != 0 or running.stdout.strip() != "true":
        port = os.environ.get("WEB_GATEWAY_PORT", "8099").strip() or "8099"
        return f"http://127.0.0.1:{port}"
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


def gateway_is_running() -> bool:
    proc = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


_REJECTION_HINTS = re.compile(
    r"non-public|redirect blocked|unsupported URL scheme|unauthorized|"
    r"body too large|max_body|extraction cap|timeout|redirect loop|too many redirects|"
    r"rate limit|forbidden|not permitted|blocked",
    re.I,
)


def _pass_if_rejected(is_error: bool, text: str, *, allow_capped_ok: bool = False) -> Outcome:
    if is_error:
        return "PASS"
    if allow_capped_ok and (
        "extraction cap" in text.lower() or "max_body" in text.lower() or "longer than" in text.lower()
    ):
        return "PASS"
    if _REJECTION_HINTS.search(text):
        return "PASS"
    return "FAIL"


def _mcp_error_message(exc: BaseException) -> str:
    if isinstance(exc, MCPError):
        return str(exc.message)
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            msg = _mcp_error_message(sub)
            if msg:
                return msg
    return str(exc)


class GatewayClient:
    def __init__(self, base: str, token: str | None, *, pause_s: float = 1.25) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.pause_s = pause_s
        self._lock = asyncio.Lock()

    async def _pause(self) -> None:
        await asyncio.sleep(self.pause_s)

    async def read_url(self, url: str, *, token: str | None = None) -> tuple[bool, str]:
        async with self._lock:
            await self._pause()
            auth = token if token is not None else self.token
            headers = {"Authorization": f"Bearer {auth}"} if auth else {}
            try:
                async with httpx.AsyncClient(headers=headers, timeout=120.0) as http_client:
                    async with streamable_http_client(f"{self.base}/", http_client=http_client) as (
                        read_stream,
                        write_stream,
                    ):
                        async with ClientSession(read_stream, write_stream) as session:
                            await session.initialize()
                            res = await session.call_tool("searxng_read_url", {"url": url})
                            text = "".join(
                                block.text for block in res.content if hasattr(block, "text")
                            )
                            return res.is_error, text
            except (MCPError, BaseExceptionGroup) as exc:
                return True, _mcp_error_message(exc)

    async def web_search(self, query: str, *, token: str | None = None) -> tuple[bool, str]:
        async with self._lock:
            await self._pause()
            auth = token if token is not None else self.token
            headers = {"Authorization": f"Bearer {auth}"} if auth else {}
            try:
                async with httpx.AsyncClient(headers=headers, timeout=120.0) as http_client:
                    async with streamable_http_client(f"{self.base}/", http_client=http_client) as (
                        read_stream,
                        write_stream,
                    ):
                        async with ClientSession(read_stream, write_stream) as session:
                            await session.initialize()
                            res = await session.call_tool(
                                "searxng_web_search",
                                {"query": query, "num_results": 3},
                            )
                            text = "".join(
                                block.text for block in res.content if hasattr(block, "text")
                            )
                            return res.is_error, text
            except (MCPError, BaseExceptionGroup) as exc:
                return True, _mcp_error_message(exc)

    async def mcp_initialize_status(self, *, token: str | None = None) -> tuple[int, str]:
        async with self._lock:
            await self._pause()
            headers: dict[str, str] = {"Accept": "application/json, text/event-stream"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            payload = {
                "jsonrpc": "2.0",
                "method": "initialize",
                "id": 1,
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "web-attack-suite", "version": "1.0"},
                },
            }
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(f"{self.base}/", headers=headers, json=payload)
                return resp.status_code, resp.text[:500]


async def run_cases(client: GatewayClient, *, skip_live: bool = False) -> list[CaseResult]:
    results: list[CaseResult] = []

    def add(
        case_id: str,
        category: ProbeKind,
        description: str,
        outcome: Outcome,
        relay_response: str,
        note: str = "",
    ) -> None:
        results.append(
            CaseResult(
                case_id=case_id,
                category=category,
                description=description,
                outcome=outcome,
                relay_response=relay_response[:2000],
                note=note,
            )
        )

    if skip_live:
        add(
            "gateway.live",
            "ssrf",
            "knowledge-gateway container running",
            "SKIP",
            "skip_live=True",
            "offline mode",
        )
        return results

    ssrf_urls: list[tuple[str, str]] = [
        ("ssrf.127", "http://127.0.0.1:8090/"),
        ("ssrf.localhost", "http://localhost:8090/"),
        ("ssrf.metadata", "http://169.254.169.254/latest/meta-data/"),
        ("ssrf.tailscale-meili", "http://198.51.100.83:7700/"),
        ("ssrf.host-docker", "http://host.docker.internal:8081/v1/models"),
        ("ssrf.ipv6-loopback", "http://[::1]:8099/"),
        ("ssrf.hex-loopback", "http://0x7f000001:8090/"),
        ("ssrf.nip-rebind", "http://127.0.0.1.nip.io:8090/"),
    ]
    for case_id, url in ssrf_urls:
        err, text = await client.read_url(url)
        add(
            case_id,
            "ssrf",
            f"read_url blocks private/special target ({url})",
            _pass_if_rejected(err, text),
            text,
        )

    err, text = await client.read_url(
        "https://httpbin.org/redirect-to?url=http%3A%2F%2F127.0.0.1%3A8090%2F&status_code=302"
    )
    add(
        "ssrf.redirect-to-private",
        "ssrf",
        "public URL 302 redirect to 127.0.0.1 refused at redirect hop",
        _pass_if_rejected(err, text),
        text,
    )

    scheme_urls: list[tuple[str, str]] = [
        ("scheme.file", "file:///etc/passwd"),
        ("scheme.userinfo", "http://user:pass@127.0.0.1:8090/"),
        ("scheme.nonstandard-port", "http://example.com:8080/"),
    ]
    for case_id, url in scheme_urls:
        err, text = await client.read_url(url)
        add(
            case_id,
            "scheme",
            f"URL scheme / shape abuse ({url})",
            _pass_if_rejected(err, text),
            text,
        )

    status, body = await client.mcp_initialize_status(token=None)
    add(
        "auth.no-token",
        "auth",
        "MCP initialize without bearer token",
        "PASS" if status == 401 else "FAIL",
        f"HTTP {status}: {body}",
    )
    status, body = await client.mcp_initialize_status(token="wrong-token-" + "x" * 32)
    add(
        "auth.bad-token",
        "auth",
        "MCP initialize with invalid bearer token",
        "PASS" if status == 401 else "FAIL",
        f"HTTP {status}: {body}",
    )
    err, text = await client.read_url("https://example.com/", token="wrong-token-" + "x" * 32)
    add(
        "auth.bad-token-tool",
        "auth",
        "searxng_read_url with invalid bearer token",
        "PASS" if err or "unauthorized" in text.lower() else "FAIL",
        text[:500],
    )

    async with httpx.AsyncClient(timeout=30.0) as raw:
        post = await raw.post(
            f"{client.base}/",
            headers={
                "Authorization": f"Bearer {client.token}",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 99,
                "params": {
                    "name": "searxng_read_url",
                    "arguments": {"url": "https://example.com/"},
                },
            },
        )
    post_ok = post.status_code in (400, 401, 404, 405, 406) or (
        post.status_code == 200 and "error" in post.text.lower()
    )
    add(
        "method.post-tool-call",
        "method",
        "unsolicited POST tools/call without MCP session handshake",
        "PASS" if post_ok else "FAIL",
        f"HTTP {post.status_code}: {post.text[:400]}",
    )

    err, text = await client.read_url("https://httpbin.org/bytes/52428800")
    add(
        "resource.50mb",
        "resource",
        "~50 MB response body bounded or rejected (MAX_BODY_BYTES / extract cap)",
        _pass_if_rejected(err, text, allow_capped_ok=True),
        text[:800],
        note="PASS if error or extraction-cap note without full download",
    )

    err, text = await client.read_url("https://httpbin.org/gzip/262144")
    add(
        "resource.gzip",
        "resource",
        "gzip-compressed payload handled without runaway memory",
        _pass_if_rejected(err, text, allow_capped_ok=True),
        text[:800],
    )

    err, text = await client.read_url("https://httpbin.org/delay/35")
    add(
        "resource.slow",
        "resource",
        "slow-drip / long server delay (35s) ends in timeout or bounded success",
        "PASS"
        if err or "timeout" in text.lower() or "sec:fence" in text
        else "FAIL",
        text[:800],
    )

    err, text = await client.read_url(
        "https://httpbin.org/redirect/10?url=https://httpbin.org/get"
    )
    add(
        "resource.redirect-depth",
        "resource",
        "deep redirect chain limited or rejected",
        _pass_if_rejected(err, text, allow_capped_ok=True),
        text[:800],
    )

    err, text = await client.read_url("https://httpbin.org/html")
    fenced = "sec:fence" in text and "untrusted" in text.lower()
    add(
        "injection.fence",
        "injection",
        "fetched HTML wrapped in signed untrusted fence (inbound prompt injection)",
        "PASS" if (not err and fenced) or _pass_if_rejected(err, text) == "PASS" else "FAIL",
        text[:600],
    )

    add(
        "scope.read-without-search",
        "scope",
        "read URL not from this turn search/message (vself client policy)",
        "SKIP",
        "relay allows explicit read_url by design",
        note="enforce in vself gateway client (W-5); not relay MCP",
    )

    leak_query = "Remember journal phrase SECRET_VSELF_LEAK_PROBE_42 and PersonName Alice Example"
    err, text = await client.web_search(leak_query)
    leaked = "SECRET_VSELF_LEAK_PROBE_42" in text or "Alice Example" in text
    add(
        "leak.query-echo",
        "leak",
        "outbound search query scrubber (private terms not echoed verbatim)",
        "SKIP" if err else ("FAIL" if leaked else "PASS"),
        text[:800],
        note="relay may echo query; vself scrubber is upstream",
    )

    return results


def render_markdown(report: SuiteReport) -> str:
    lines = [
        "# Web attack suite results (knowledge-gateway)",
        "",
        f"- Generated: `{report.generated_at}`",
        f"- Gateway: `{report.gateway_base}` (running={report.gateway_running})",
        "",
        "| Case | Category | Outcome | Description | Relay response (truncated) | Note |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in report.results:
        resp = r.relay_response.replace("|", "\\|").replace("\n", " ")[:160]
        note = r.note.replace("|", "\\|")
        desc = r.description.replace("|", "\\|")
        lines.append(
            f"| `{r.case_id}` | {r.category} | **{r.outcome}** | {desc} | {resp} | {note} |"
        )
    summary = report.to_dict()["summary"]
    lines.extend(
        [
            "",
            f"**Summary:** PASS={summary['pass']} FAIL={summary['fail']} SKIP={summary['skip']}",
            "",
            "Re-run: `cd /opt/virtual-self-memory && .venv/bin/python scripts/web_attack_suite.py`",
        ]
    )
    return "\n".join(lines) + "\n"


async def _async_main(args: argparse.Namespace) -> int:
    base = gateway_base_url()
    if not base:
        print("knowledge-gateway base URL unavailable", file=sys.stderr)
        return 2
    token = load_gateway_token()
    if not token and not args.skip_live:
        print("WEB_GATEWAY_MCP_TOKEN missing", file=sys.stderr)
        return 2

    client = GatewayClient(base, token, pause_s=args.pause)
    results = await run_cases(client, skip_live=args.skip_live)
    report = SuiteReport(
        generated_at=datetime.now(UTC).isoformat(),
        gateway_base=base,
        gateway_running=gateway_is_running(),
        results=results,
    )

    out_json = Path(args.json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")

    md_path = Path(args.markdown)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_markdown(report), encoding="utf-8")

    summary = report.to_dict()["summary"]
    print(
        f"Wrote {out_json} and {md_path} — "
        f"PASS={summary['pass']} FAIL={summary['fail']} SKIP={summary['skip']}"
    )
    return 1 if summary["fail"] else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MD)
    parser.add_argument("--pause", type=float, default=1.25, help="seconds between MCP calls")
    parser.add_argument("--skip-live", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_async_main(args)))


if __name__ == "__main__":
    main()
