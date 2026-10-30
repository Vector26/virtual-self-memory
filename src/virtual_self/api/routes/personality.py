from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from virtual_self.api.deps import auth_with_scope
from virtual_self.auth.credentials import AuthContext
from virtual_self.services.personality import bootstrap_personality, compile_context, get_active_personality

router = APIRouter(tags=["personality"])


class ContextCompileBody(BaseModel):
    query: str = Field(default="", max_length=500)
    memory_limit: int = Field(default=12, ge=1, le=50)
    token_budget: int = Field(default=4000, ge=256, le=32000)


@router.get("/v1/personality")
def personality_get(auth: AuthContext = Depends(auth_with_scope("personality.read"))):
    return get_active_personality(auth)


@router.post("/v1/personality/bootstrap")
def personality_bootstrap(auth: AuthContext = Depends(auth_with_scope("personality.approve"))):
    return bootstrap_personality(auth)


@router.post("/v1/context:compile")
def context_compile(
    body: ContextCompileBody,
    auth: AuthContext = Depends(auth_with_scope("context.read")),
):
    return compile_context(
        auth,
        body.query,
        memory_limit=body.memory_limit,
        token_budget=body.token_budget,
    )
