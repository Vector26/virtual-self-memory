from __future__ import annotations

from fastapi import APIRouter, Depends

from virtual_self.api.deps import auth_with_scope
from virtual_self.auth.credentials import AuthContext
from virtual_self.services.indexing import index_stats, reindex_subject

router = APIRouter(prefix="/v1/index", tags=["index"])


@router.get("/status")
def status(auth: AuthContext = Depends(auth_with_scope("memory.read"))):
    return index_stats(auth.subject_id)


@router.post("/rebuild")
def rebuild(auth: AuthContext = Depends(auth_with_scope("admin.import"))):
    return reindex_subject(auth.subject_id, only_missing=False)
