"""Read-only web knowledge (Phase W) — vself-facing gateway client."""

from virtual_self.web.client import (
    KnowledgeGatewayClient,
    ReadResult,
    SearchResult,
    WebKnowledgeTurn,
    WebKnowledgeError,
)
from virtual_self.web.scrub import QueryScrubber, ScrubResult, build_query_scrubber

__all__ = [
    "KnowledgeGatewayClient",
    "QueryScrubber",
    "ReadResult",
    "ScrubResult",
    "SearchResult",
    "WebKnowledgeTurn",
    "WebKnowledgeError",
    "build_query_scrubber",
]
