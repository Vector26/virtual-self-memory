"""W-7: outbound search query scrubber — nothing private leaves."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from virtual_self.web.client import KnowledgeGatewayClient, WebKnowledgeTurn, WebKnowledgeError
from virtual_self.web.scrub import (
    QueryScrubber,
    contains_blocked_secret,
    extract_private_phrases,
    load_scrubber_from_fixture,
    name_from_notion_segment,
    names_from_source_path,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "web_scrub_corpus.json"


@pytest.fixture
def corpus() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def scrubber(corpus: dict) -> QueryScrubber:
    return QueryScrubber(
        blocked_names=tuple(corpus["blocked_names"]),
        private_phrases=tuple(corpus["private_phrases"]),
    )


def test_notion_people_path_extracts_names():
    path = "People [a51decc3]/People [50016a98]/Jordan Example [22e0a991]/index.md"
    assert names_from_source_path(path) == ["Jordan Example"]
    assert name_from_notion_segment("Ananya Sharma [abc12345]") == "Ananya Sharma"


def test_scrub_redacts_email_phone_address(corpus: dict, scrubber: QueryScrubber):
    q = (
        f"weather near {corpus['addresses'][0]} contact {corpus['emails'][0]} "
        f"or {corpus['phones'][0]}"
    )
    res = scrubber.scrub(q)
    assert corpus["emails"][0] not in res.scrubbed
    assert corpus["phones"][0] not in res.scrubbed
    assert "Evergreen" not in res.scrubbed
    assert "[EMAIL]" in res.scrubbed
    assert "[PHONE]" in res.scrubbed
    assert "[ADDRESS]" in res.scrubbed
    assert not res.blocked


def test_scrub_redacts_people_names_and_journal_phrases(scrubber: QueryScrubber, corpus: dict):
    name = corpus["blocked_names"][0]
    phrase = corpus["private_phrases"][0]
    q = f"Search news about {name} and also {phrase} trends"
    res = scrubber.scrub(q)
    assert name.lower() not in res.scrubbed.lower()
    assert phrase.split()[0] not in res.scrubbed.lower() or "[PRIVATE]" in res.scrubbed
    for word in phrase.split():
        assert word not in res.scrubbed.lower() or "[PRIVATE]" in res.scrubbed


def test_scrub_blocks_query_that_is_only_private(scrubber: QueryScrubber, corpus: dict):
    q = corpus["blocked_names"][1]
    res = scrubber.scrub(q)
    assert res.blocked


def test_extract_private_phrases_min_five_words():
    text = "one two three four five six seven eight"
    phrases = extract_private_phrases([text], min_words=5)
    assert "one two three four five" in phrases
    assert "five six seven eight" not in phrases or len("five six seven eight".split()) >= 5


def test_adversarial_corpus_audit_zero_leaks(scrubber: QueryScrubber, corpus: dict):
    """200 synthetic adversarial queries — scrubbed output must not echo secrets."""
    secrets: list[str] = []
    secrets.extend(corpus["blocked_names"])
    secrets.extend(corpus["private_phrases"])
    secrets.extend(corpus["emails"])
    secrets.extend(corpus["phones"])
    secrets.extend(corpus["addresses"])

    templates = corpus["adversarial_templates"]
    leaks = 0
    for i in range(200):
        secret = secrets[i % len(secrets)]
        template = templates[i % len(templates)]
        raw = template.format(secret=secret)
        if i % 7 == 0:
            raw = raw.upper()
        if i % 11 == 0:
            raw = raw.replace(" ", "  ")
        res = scrubber.scrub(raw)
        if res.blocked:
            continue
        hit = contains_blocked_secret(res.scrubbed, [secret])
        if hit:
            leaks += 1
    assert leaks == 0, f"{leaks}/200 scrubbed queries still contained a secret"


def test_load_scrubber_from_fixture():
    s = load_scrubber_from_fixture(FIXTURE)
    assert "Jordan Example" in s.blocked_names


@pytest.mark.asyncio
async def test_client_sends_scrubbed_query_to_gateway(monkeypatch, tmp_path: Path, corpus: dict):
    scrubber = QueryScrubber(
        blocked_names=tuple(corpus["blocked_names"][:2]),
        private_phrases=tuple(corpus["private_phrases"][:1]),
    )
    turn = WebKnowledgeTurn(turn_id="scrub-1")
    client = KnowledgeGatewayClient(
        base_url="http://127.0.0.1:9",
        token="x" * 32,
        audit_path=tmp_path / "audit.jsonl",
        scrubber=scrubber,
    )
    seen: dict[str, str] = {}

    async def fake_tool(name: str, arguments: dict):
        if name == "searxng_web_search":
            seen["query"] = arguments["query"]
            return False, (
                '<sec:fence rating="untrusted">\n'
                "Title: Example\nURL: https://example.com/\nSnippet: ok\n</sec:fence>"
            )
        raise AssertionError(name)

    monkeypatch.setattr(client, "_mcp_tool", fake_tool)
    secret_name = corpus["blocked_names"][0]
    await client.search(turn, f"latest news about {secret_name} tariffs")
    assert secret_name not in seen["query"]
    assert "[NAME]" in seen["query"]

    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    row = json.loads(lines[-1])
    assert secret_name in row["query"]
    assert secret_name not in row["query_scrubbed"]


@pytest.mark.asyncio
async def test_client_blocks_fully_private_query(monkeypatch, corpus: dict):
    scrubber = QueryScrubber(blocked_names=tuple(corpus["blocked_names"]), private_phrases=())
    client = KnowledgeGatewayClient(
        base_url="http://127.0.0.1:9",
        token="x" * 32,
        scrubber=scrubber,
    )

    async def fake_tool(name: str, arguments: dict):
        raise AssertionError("gateway must not be called")

    monkeypatch.setattr(client, "_mcp_tool", fake_tool)
    turn = WebKnowledgeTurn(turn_id="scrub-2")
    with pytest.raises(WebKnowledgeError, match="blocked after privacy scrub"):
        await client.search(turn, corpus["blocked_names"][0])
