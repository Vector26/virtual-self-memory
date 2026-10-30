from __future__ import annotations

from fastapi import Header, Request

from virtual_self.auth.credentials import AuthContext, require_scope, resolve_auth


def auth_with_scope(scope: str):
    def dependency(authorization: str | None = Header(default=None)) -> AuthContext:
        auth = resolve_auth(authorization)
        require_scope(auth, scope)
        return auth

    return dependency


def request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")
