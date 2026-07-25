"""Tests del healthcheck.

Con la infraestructura abajo (el caso por defecto en unit tests) el endpoint
debe devolver 503 y el detalle por dependencia. Los estados `ok` y `degradado`
se prueban parcheando los pings.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from httpx import ASGITransport, AsyncClient

from api.app import create_app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _fake_ping(result: bool) -> Callable[[], Awaitable[bool]]:
    async def _ping() -> bool:
        return result

    return _ping


@pytest.mark.usefixtures("dead_db", "dead_redis")
async def test_healthz_returns_503_when_database_is_down(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 503
    body = response.json()
    assert body["estado"] == "caido"
    assert body["dependencias"]["db"] == {"ok": False, "critica": True}
    assert body["dependencias"]["redis"] == {"ok": False, "critica": False}


async def test_healthz_returns_200_when_all_dependencies_are_up(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("api.routers.health.db.check_connection", _fake_ping(True))
    monkeypatch.setattr("api.routers.health.redis.ping", _fake_ping(True))

    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["estado"] == "ok"


async def test_healthz_is_degraded_but_healthy_without_redis(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redis caído no tumba la API: sirve con cache frío."""
    monkeypatch.setattr("api.routers.health.db.check_connection", _fake_ping(True))
    monkeypatch.setattr("api.routers.health.redis.ping", _fake_ping(False))

    response = await client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["estado"] == "degradado"
    assert body["dependencias"]["redis"]["ok"] is False


async def test_openapi_schema_is_served(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    assert "/healthz" in response.json()["paths"]
