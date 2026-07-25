"""Application factory de la API.

La API es **interna**: sólo la consumen el BFF de Astro y el admin, siempre con
`X-API-Key` (fase 4). Nunca se publica a internet — Caddy expone el servicio
`web`, no éste. Ver §14 del foundation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.routers import health, instituciones, meta
from core import db, redis
from core.logging import configure_logging, get_logger
from core.settings import settings

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    log.info("api_startup", entorno=settings.environment, puerto=settings.api_port)
    yield
    await db.dispose_engine()
    await redis.close()
    log.info("api_shutdown")


def create_app() -> FastAPI:
    """Construye la aplicación. Una factory permite instanciarla en tests."""
    configure_logging()

    app = FastAPI(
        title="Brújula Financiera — API",
        description=(
            "Comparador de instrumentos de ahorro e inversión en México. "
            "API interna: la consumen el BFF y el admin, nunca el navegador."
        ),
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    if settings.cors_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["X-API-Key", "Content-Type"],
        )

    app.include_router(health.router)
    app.include_router(meta.router)
    app.include_router(instituciones.router)

    return app


__all__ = ["create_app"]
