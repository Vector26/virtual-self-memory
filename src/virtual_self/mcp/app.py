from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from virtual_self.auth.credentials import resolve_auth
from virtual_self.config import get_settings
from virtual_self.persistence.db import init_pool
from virtual_self.services.memories import create_memory, get_memory, search_memories
from virtual_self.services.personality import compile_context, get_active_personality

mcp = FastMCP("virtual-self-memory")


def _auth():
    token = os.environ.get("VSM_MCP_TOKEN") or (
        get_settings().bootstrap_token.get_secret_value() if get_settings().bootstrap_token else None
    )
    if not token:
        raise RuntimeError("set VSM_MCP_TOKEN or VSM_BOOTSTRAP_TOKEN")
    return resolve_auth(f"Bearer {token}")


@mcp.tool()
def self_context(query: str = "") -> str:
    """Return approved personality + relevant memories for answering as Default."""
    init_pool()
    auth = _auth()
    bundle = compile_context(auth, query=query)
    return json.dumps(bundle, indent=2)


@mcp.tool()
def personality_get() -> str:
    """Return active personality facets."""
    init_pool()
    return json.dumps(get_active_personality(_auth()), indent=2)


@mcp.tool()
def memory_search(query: str, limit: int = 10) -> str:
    """Search autobiographical memories."""
    init_pool()
    auth = _auth()
    items = search_memories(auth, query, limit=min(limit, 25))
    return json.dumps({"items": items}, indent=2)


@mcp.tool()
def memory_get(memory_id: str) -> str:
    """Fetch one memory by id."""
    init_pool()
    from uuid import UUID

    return json.dumps(get_memory(_auth(), UUID(memory_id)), indent=2)


@mcp.tool()
def memory_create(kind: str, content: str) -> str:
    """Record a user-asserted memory (preference, goal, episode)."""
    init_pool()
    return json.dumps(create_memory(_auth(), kind=kind, content=content), indent=2)


def main() -> None:
    configure = __import__(
        "virtual_self.observability.logging", fromlist=["configure_logging"]
    ).configure_logging
    configure(get_settings().log_level)
    init_pool()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
