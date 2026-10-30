from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from virtual_self.api.deps import auth_with_scope, request_id
from virtual_self.auth.credentials import AuthContext
from virtual_self.config import get_settings
from virtual_self.services.my_life_import import plan_import, run_import

router = APIRouter(prefix="/v1/imports/my-life", tags=["imports"])


class MyLifeImportBody(BaseModel):
    dry_run: bool = Field(default=True)
    promote_memories: bool = Field(default=True)
    root: str | None = Field(default=None, description="Override VSM_MY_LIFE_ROOT")


@router.post("/plan")
def import_plan(
    body: MyLifeImportBody | None = None,
    auth: AuthContext = Depends(auth_with_scope("admin.import")),
):
    settings = get_settings()
    root = Path((body.root if body and body.root else str(settings.my_life_root)))
    return plan_import(root)


@router.post("")
def import_run(
    body: MyLifeImportBody,
    request: Request,
    auth: AuthContext = Depends(auth_with_scope("admin.import")),
):
    settings = get_settings()
    root = Path(body.root or str(settings.my_life_root))
    return run_import(
        auth,
        root,
        dry_run=body.dry_run,
        promote_memories=body.promote_memories,
        request_id=request_id(request),
    )
