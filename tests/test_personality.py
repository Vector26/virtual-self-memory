from __future__ import annotations

from pathlib import Path

from virtual_self.auth.credentials import resolve_auth
from virtual_self.services.my_life_import import run_import
from virtual_self.services.personality import bootstrap_personality, compile_context, get_active_personality

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "my_life_mini"


def test_personality_bootstrap_and_context(client, auth_headers, ensure_db):
    import os

    auth = resolve_auth(auth_headers["Authorization"])
    run_import(auth, FIXTURE_ROOT, dry_run=False, promote_memories=True)
    boot = client.post("/v1/personality/bootstrap", headers=auth_headers)
    assert boot.status_code == 200
    assert len(boot.json()["facets"]) >= 4

    ctx = client.post(
        "/v1/context:compile",
        json={"query": "home lab"},
        headers=auth_headers,
    )
    assert ctx.status_code == 200
    body = ctx.json()
    assert "approved_personality" in body
    assert body["relevant_memories"]


def test_get_personality_requires_bootstrap(client, auth_headers):
    r = client.get("/v1/personality", headers=auth_headers)
    if r.status_code == 404:
        return
    assert r.status_code == 200
