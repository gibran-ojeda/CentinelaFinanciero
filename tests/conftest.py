"""Configuración global de pytest.

`core.settings` instancia el singleton `settings` **al importarse**, así que las
variables de entorno tienen que estar puestas antes de que cualquier test (o
cualquier módulo que ellos importen) toque `core`. Por eso esto vive arriba del
archivo y no en una fixture.
"""

from __future__ import annotations

import os

_TEST_ENV: dict[str, str] = {
    "ENVIRONMENT": "test",
    "POSTGRES_HOST": "127.0.0.1",
    "POSTGRES_PORT": "5433",
    "POSTGRES_DB": "brujula_test",
    "POSTGRES_USER": "brujula",
    "POSTGRES_PASSWORD": "test-password",
    "REDIS_HOST": "127.0.0.1",
    "REDIS_PORT": "6380",
    "REDIS_DB": "15",
    "LOG_LEVEL": "WARNING",
    "API_READ_KEY": "test-read-key",
    "API_ADMIN_KEY": "test-admin-key",
}

for _key, _value in _TEST_ENV.items():
    os.environ.setdefault(_key, _value)

# Nota de precedencia: pydantic-settings resuelve variables de entorno **antes**
# que el `.env`, así que estos valores ganan sobre el `.env` del desarrollador
# (y sobre el `cp .env.example .env` que hace el CI) sin necesidad de borrarlo.

import subprocess  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from functools import lru_cache  # noqa: E402

import pytest  # noqa: E402


@lru_cache(maxsize=1)
def docker_available() -> bool:
    """¿Hay un daemon de Docker con el que testcontainers pueda hablar?

    Los tests que necesitan infraestructura real se **saltan** si no lo hay, de
    modo que `pytest` sigue pasando en una máquina sin Docker levantado
    (criterio de aceptación de la fase 1) y corre completo cuando sí lo hay.
    """
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Salta los tests marcados `requires_docker` si no hay daemon.

    Se resuelve con un hook y no con un `skipif` importable para que los
    módulos de test no tengan que importar nada de `tests/` (que no es un
    paquete instalable).
    """
    if docker_available():
        return
    skip = pytest.mark.skip(reason="requiere un daemon de Docker para testcontainers")
    for item in items:
        if "requires_docker" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def redis_container_url() -> Iterator[str]:
    """Redis real y efímero para los tests que no pueden usar un doble."""
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


@pytest.fixture
async def real_redis(redis_container_url: str) -> Iterator[None]:
    """Apunta `core.redis` al contenedor durante el test."""
    from redis.asyncio import Redis

    import core.redis as redis_module

    previous = redis_module._client
    redis_module._client = Redis.from_url(redis_container_url, decode_responses=True)
    try:
        await redis_module._client.flushdb()
        yield
    finally:
        await redis_module._client.aclose()
        redis_module._client = previous
