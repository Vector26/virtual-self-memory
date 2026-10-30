from __future__ import annotations

import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from virtual_self.observability.metrics import HTTP_LATENCY, HTTP_REQUESTS


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id, path=request.url.path)
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-Id"] = request_id
            return response
        finally:
            route = request.scope.get("route")
            route_name = getattr(route, "path", request.url.path)
            elapsed = time.perf_counter() - start
            HTTP_REQUESTS.labels(request.method, route_name, str(status)).inc()
            HTTP_LATENCY.labels(request.method, route_name).observe(elapsed)
