from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response

from virtual_self.api.deps import auth_with_scope, request_id
from virtual_self.api.schemas import MemoryCorrect, MemoryCreate, MemorySearch
from virtual_self.auth.credentials import AuthContext
from virtual_self.services import idempotency
from virtual_self.services.memories import (
    correct_memory,
    create_memory,
    delete_memory,
    forget_memory,
    get_memory,
    search_memories,
)

router = APIRouter(prefix="/v1/memories", tags=["memories"])


@router.post("")
def post_memory(
    body: MemoryCreate,
    request: Request,
    response: Response,
    auth: AuthContext = Depends(auth_with_scope("memory.write")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    rid = request_id(request)
    payload = body.model_dump()
    if idempotency_key:
        cached = idempotency.get_cached(idempotency_key, auth.actor_id, payload)
        if cached:
            status, body_out = cached
            response.status_code = status
            return body_out
    out = create_memory(
        auth,
        kind=body.kind,
        content=body.content,
        scope=body.scope,
        trust=body.trust,
        sensitivity=body.sensitivity,
        request_id=rid,
    )
    if idempotency_key:
        idempotency.store_cached(idempotency_key, auth.actor_id, payload, 201, out)
    response.status_code = 201
    return out


@router.get("/{memory_id}")
def get_one(
    memory_id: UUID,
    auth: AuthContext = Depends(auth_with_scope("memory.read")),
):
    return get_memory(auth, memory_id)


@router.post("/search")
def search(
    body: MemorySearch,
    auth: AuthContext = Depends(auth_with_scope("memory.read")),
):
    return {"items": search_memories(auth, body.query, body.limit)}


@router.post("/{memory_id}:correct")
def correct(
    memory_id: UUID,
    body: MemoryCorrect,
    request: Request,
    auth: AuthContext = Depends(auth_with_scope("memory.write")),
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    return correct_memory(
        auth,
        memory_id,
        body.content,
        if_match,
        request_id=request_id(request),
    )


@router.post("/{memory_id}:forget")
def forget(
    memory_id: UUID,
    request: Request,
    auth: AuthContext = Depends(auth_with_scope("memory.write")),
):
    return forget_memory(auth, memory_id, request_id=request_id(request))


@router.delete("/{memory_id}")
def delete(
    memory_id: UUID,
    request: Request,
    auth: AuthContext = Depends(auth_with_scope("memory.delete")),
):
    delete_memory(auth, memory_id, request_id=request_id(request))
    return Response(status_code=204)
