from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from virtual_self.api.errors import virtual_self_exception_handler
from virtual_self.api.middleware import RequestContextMiddleware
from virtual_self.api.routes import chat, feedback, health, imports, indexing, memories, personality, proposals
from virtual_self.config import get_settings
from virtual_self.domain.errors import VirtualSelfError
from virtual_self.observability.logging import configure_logging, get_logger
from virtual_self.persistence.db import close_pool, init_pool

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    init_pool()
    log.info("vsm_started", host=settings.api_host, port=settings.api_port)
    yield
    close_pool()
    log.info("vsm_stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Virtual Self Memory API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_exception_handler(VirtualSelfError, virtual_self_exception_handler)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router)
    app.include_router(memories.router)
    app.include_router(imports.router)
    app.include_router(personality.router)
    app.include_router(chat.router)
    app.include_router(feedback.router)
    app.include_router(indexing.router)
    app.include_router(proposals.router)

    settings = get_settings()
    if settings.metrics_enabled:
        Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

    return app


app = create_app()
