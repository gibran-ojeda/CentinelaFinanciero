"""Healthcheck del servicio API.

Distingue entre dependencias **críticas** y **degradables**: sin Postgres no
hay nada que servir (503), pero sin Redis la API sigue respondiendo con cache
frío (200 con `redis: false`). El compose y el CD leen este endpoint.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field

from core import db, redis
from core.settings import settings

router = APIRouter(tags=["meta"])


class DependencyHealth(BaseModel):
    ok: bool
    critica: bool = Field(description="Si es False, su caída no vuelve 503 el healthcheck")


class HealthResponse(BaseModel):
    estado: Literal["ok", "degradado", "caido"]
    servicio: str
    entorno: str
    dependencias: dict[str, DependencyHealth]


@router.get(
    "/healthz",
    response_model=HealthResponse,
    summary="Estado del servicio y de sus dependencias",
)
async def healthz(response: Response) -> HealthResponse:
    db_ok = await db.check_connection()
    redis_ok = await redis.ping()

    if not db_ok:
        estado: Literal["ok", "degradado", "caido"] = "caido"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif not redis_ok:
        estado = "degradado"
    else:
        estado = "ok"

    return HealthResponse(
        estado=estado,
        servicio=settings.app_name,
        entorno=settings.environment,
        dependencias={
            "db": DependencyHealth(ok=db_ok, critica=True),
            "redis": DependencyHealth(ok=redis_ok, critica=False),
        },
    )
