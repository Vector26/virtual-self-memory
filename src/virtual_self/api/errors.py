from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from virtual_self.domain.errors import VirtualSelfError


def problem_response(exc: VirtualSelfError, request_id: str | None) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "type": f"https://virtual-self.local/errors/{exc.code}",
            "title": exc.code,
            "status": exc.status_code,
            "detail": exc.message,
            "instance": request_id,
            "errors": exc.details,
        },
    )


async def virtual_self_exception_handler(request: Request, exc: VirtualSelfError) -> JSONResponse:
    rid = getattr(request.state, "request_id", None)
    return problem_response(exc, rid)
