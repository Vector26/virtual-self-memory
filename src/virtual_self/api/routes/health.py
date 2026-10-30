from __future__ import annotations

from fastapi import APIRouter

from virtual_self import __version__
from virtual_self.persistence.db import health_check

router = APIRouter(tags=["health"])


@router.get("/health/live")
def live():
    return {"status": "ok", "version": __version__}


@router.get("/health/ready")
def ready():
    db_ok = health_check()
    status = "ok" if db_ok else "degraded"
    code = 200 if db_ok else 503
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=code,
        content={"status": status, "checks": {"database": db_ok}},
    )
