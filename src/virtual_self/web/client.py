from __future__ import annotations

import hashlib
import html
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from virtual_self.config import get_settings
from virtual_self.observability.logging import get_logger
from virtual_self.web.scrub import QueryScrubber, ScrubResult, build_query_scrubber

log = get_logger(__name__)

MAX_SEARCHES_PER_TURN = 3
MAX_READS_PER_TURN = 3
MAX_RESULTS_PER_SEARCH = 8
MAX_EXTRACTED_CHARS = 20_000
_CACHE_TTL_SECONDS = 24 * 3600

_USER_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
# The relay preamble mentions a bare `<sec:fence>` inline; the real payload fence
# always carries attributes (xmlns/signature/nonce). Require whitespace+attrs.
_FENCE_INNER = re.compile(r"<sec:fence\s[^>]*>(.*?)</sec:fence>", re.DOTALL | re.IGNORECASE)
_SEARCH_BLOCK = re.compile(
    r"Title:\s*(?P<title>.+?)\nURL:\s*(?P<url>\S+)"
    r"(?:\nSnippet:\s*(?P<snippet>.*?))?(?:\nEngines:\s*(?P<engines>[^\n]+))?",
    re.DOTALL,
)


class WebKnowledgeError(Exception):
    """Policy or gateway failure for web search/read."""


@dataclass(frozen=True)
class SearchResult:
    result_id: str
    title: str
    url: str
    snippet: str
    engine: str | None = None
    rank: int = 0


@dataclass(frozen=True)
class ReadResult:
    title: str
    final_url: str
    text: str
    fetched_at: str
    content_hash: str


@dataclass
class WebKnowledgeTurn:
    turn_id: str
    search_count: int = 0
    read_count: int = 0
    results_by_id: dict[str, SearchResult] = field(default_factory=dict)
    allowed_urls: set[str] = field(default_factory=set)
    user_pasted_urls: set[str] = field(default_factory=set)


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return url.strip()
    path = parsed.path.rstrip("/") or "/"
    netloc = parsed.netloc.lower()
    return urlunparse((parsed.scheme.lower(), netloc, path, "", parsed.query, ""))


def urls_from_user_message(message: str) -> list[str]:
    return [normalize_url(u) for u in _USER_URL.findall(message)]


def _unwrap_fence(text: str) -> str:
    # Preamble may quote example fences; the payload fence is the outermost (last).
    matches = list(_FENCE_INNER.finditer(text))
    if matches:
        return html.unescape(matches[-1].group(1)).strip()
    return html.unescape(text).strip()


def parse_search_results(raw: str, *, k: int) -> list[SearchResult]:
    body = _unwrap_fence(raw)
    chunks = re.split(r"\n---+\n", body)
    out: list[SearchResult] = []
    rank = 0
    for chunk in chunks:
        m = _SEARCH_BLOCK.search(chunk.strip())
        if not m:
            continue
        rank += 1
        rid = str(rank)
        title = m.group("title").strip()
        url = normalize_url(m.group("url").strip())
        snippet = (m.group("snippet") or "").strip()
        engine = (m.group("engines") or "").strip() or None
        out.append(
            SearchResult(
                result_id=rid,
                title=title,
                url=url,
                snippet=snippet,
                engine=engine,
                rank=rank,
            )
        )
        if len(out) >= k:
            break
    return out


def parse_read_result(raw: str, *, url: str) -> ReadResult:
    text = _unwrap_fence(raw)
    if len(text) > MAX_EXTRACTED_CHARS:
        text = text[:MAX_EXTRACTED_CHARS]
    title_line = text.split("\n", 1)[0].strip() if text else url
    fetched_at = datetime.now(timezone.utc).isoformat()
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return ReadResult(
        title=title_line[:500],
        final_url=normalize_url(url),
        text=text,
        fetched_at=fetched_at,
        content_hash=content_hash,
    )


class KnowledgeGatewayClient:
    """Two-verb client in front of mcp-searxng-relay (search + scoped read)."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        audit_path: Path | None = None,
        scrubber: QueryScrubber | None = None,
        scrub_on_search: bool = True,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.web_gateway_url).rstrip("/")
        self.token = token or settings.web_gateway_token
        self.audit_path = audit_path or settings.web_audit_path
        self._scrubber = scrubber
        self._scrub_on_search = scrub_on_search
        self._search_cache: dict[str, tuple[float, list[SearchResult]]] = {}
        self._read_cache: dict[str, tuple[float, ReadResult]] = {}

    def register_user_message(self, turn: WebKnowledgeTurn, message: str) -> None:
        for url in urls_from_user_message(message):
            turn.user_pasted_urls.add(url)
            turn.allowed_urls.add(url)

    def _cache_get_search(self, key: str) -> list[SearchResult] | None:
        hit = self._search_cache.get(key)
        if not hit:
            return None
        ts, val = hit
        if time.time() - ts > _CACHE_TTL_SECONDS:
            del self._search_cache[key]
            return None
        return val

    def _cache_get_read(self, key: str) -> ReadResult | None:
        hit = self._read_cache.get(key)
        if not hit:
            return None
        ts, val = hit
        if time.time() - ts > _CACHE_TTL_SECONDS:
            del self._read_cache[key]
            return None
        return val

    def _audit(self, turn_id: str, event: str, payload: dict[str, Any]) -> None:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "turn_id": turn_id,
            "event": event,
            **payload,
        }
        path = self.audit_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    async def _mcp_tool(self, name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        if not self.token:
            return True, "WEB_GATEWAY_MCP_TOKEN not configured"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with httpx.AsyncClient(headers=headers, timeout=120.0) as http_client:
                async with streamable_http_client(f"{self.base_url}/", http_client=http_client) as (
                    read_stream,
                    write_stream,
                ):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        res = await session.call_tool(name, arguments)
                        text = "".join(
                            block.text for block in res.content if hasattr(block, "text")
                        )
                        return bool(res.is_error), text
        except (MCPError, BaseExceptionGroup, OSError) as exc:
            return True, str(exc)

    def _scrub_search_query(self, query: str) -> ScrubResult:
        if not self._scrub_on_search:
            q = query.strip()
            return ScrubResult(original=q, scrubbed=q, blocked=not q)
        scrubber = self._scrubber
        if scrubber is None:
            scrubber = build_query_scrubber()
            self._scrubber = scrubber
        return scrubber.scrub(query)

    async def search(
        self,
        turn: WebKnowledgeTurn,
        query: str,
        *,
        k: int = MAX_RESULTS_PER_SEARCH,
    ) -> list[SearchResult]:
        q_raw = query.strip()
        if not q_raw:
            raise WebKnowledgeError("empty search query")
        scrub = self._scrub_search_query(q_raw)
        if scrub.blocked:
            raise WebKnowledgeError("search query blocked after privacy scrub")
        q = scrub.scrubbed
        if turn.search_count >= MAX_SEARCHES_PER_TURN:
            raise WebKnowledgeError(f"search cap ({MAX_SEARCHES_PER_TURN}) reached for this turn")
        k = min(max(1, k), MAX_RESULTS_PER_SEARCH)
        cache_key = hashlib.sha256(f"{q}|{k}".encode()).hexdigest()
        cached = self._cache_get_search(cache_key)
        if cached is not None:
            if turn.search_count >= MAX_SEARCHES_PER_TURN:
                raise WebKnowledgeError(
                    f"search cap ({MAX_SEARCHES_PER_TURN}) reached for this turn"
                )
            turn.search_count += 1
            self._audit(
                turn.turn_id,
                "search_cache_hit",
                {"query": q_raw, "query_scrubbed": q, "k": k},
            )
            for item in cached:
                turn.results_by_id.setdefault(item.result_id, item)
                turn.allowed_urls.add(item.url)
            return cached

        is_error, raw = await self._mcp_tool(
            "searxng_web_search",
            {"query": q, "num_results": k},
        )
        self._audit(
            turn.turn_id,
            "search",
            {
                "query": q_raw,
                "query_scrubbed": q,
                "scrub_reasons": list(scrub.reasons),
                "k": k,
                "error": is_error,
            },
        )
        turn.search_count += 1
        if is_error:
            raise WebKnowledgeError(raw[:500] or "search failed")

        results = parse_search_results(raw, k=k)
        for item in results:
            turn.results_by_id[item.result_id] = item
            turn.allowed_urls.add(item.url)
        self._search_cache[cache_key] = (time.time(), results)
        return results

    def _resolve_read_url(
        self,
        turn: WebKnowledgeTurn,
        *,
        result_id: str | None = None,
        user_url: str | None = None,
    ) -> str:
        if result_id is not None:
            hit = turn.results_by_id.get(str(result_id))
            if hit is None:
                raise WebKnowledgeError(
                    "read refused: result_id not from a search in this turn"
                )
            return hit.url
        if user_url is not None:
            norm = normalize_url(user_url)
            if norm not in turn.allowed_urls and norm not in turn.user_pasted_urls:
                raise WebKnowledgeError(
                    "read refused: URL not from this turn search or user message"
                )
            return norm
        raise WebKnowledgeError("read requires result_id or user_url")

    async def read(
        self,
        turn: WebKnowledgeTurn,
        *,
        result_id: str | None = None,
        user_url: str | None = None,
    ) -> ReadResult:
        if turn.read_count >= MAX_READS_PER_TURN:
            raise WebKnowledgeError(f"read cap ({MAX_READS_PER_TURN}) reached for this turn")
        url = self._resolve_read_url(turn, result_id=result_id, user_url=user_url)
        cache_key = hashlib.sha256(url.encode()).hexdigest()
        cached = self._cache_get_read(cache_key)
        if cached is not None:
            self._audit(turn.turn_id, "read_cache_hit", {"url": url})
            return cached

        is_error, raw = await self._mcp_tool("searxng_read_url", {"url": url})
        self._audit(
            turn.turn_id,
            "read",
            {"url": url, "result_id": result_id, "error": is_error},
        )
        turn.read_count += 1
        if is_error:
            raise WebKnowledgeError(raw[:500] or "read failed")
        parsed = parse_read_result(raw, url=url)
        self._read_cache[cache_key] = (time.time(), parsed)
        return parsed


def get_knowledge_gateway_client() -> KnowledgeGatewayClient:
    return KnowledgeGatewayClient()
