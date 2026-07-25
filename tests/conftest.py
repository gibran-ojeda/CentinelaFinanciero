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
    "POSTGRES_DB": "centinela_test",
    "POSTGRES_USER": "centinela",
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

import contextlib  # noqa: E402
import socket  # noqa: E402
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


@pytest.fixture(autouse=True)
async def _cliente_redis_por_test() -> Iterator[None]:
    """Impide que el cliente Redis del módulo cruce event loops.

    `core.redis` cachea el cliente a nivel de módulo y pytest-asyncio crea un
    loop por test. Sin esto, el segundo test que toque Redis hereda conexiones
    creadas en un loop ya cerrado y falla con "Future attached to a different
    loop" — un error que no dice nada sobre el código bajo prueba.

    Las fixtures `real_redis` y `dead_redis` se desmontan antes que ésta, así
    que aquí sólo queda el cliente ambiental, si lo hubo.
    """
    yield

    import core.redis as redis_module

    cliente = redis_module._client
    redis_module._client = None
    if cliente is not None:
        with contextlib.suppress(Exception):
            await cliente.aclose()


def closed_port() -> int:
    """Un puerto TCP en el que con toda seguridad no escucha nadie.

    Los tests de degradación no pueden asumir "no hay Redis levantado": basta
    con que el desarrollador tenga el `docker compose up` corriendo para que
    los puertos de test estén ocupados por servicios reales. Apuntar a un
    puerto cerrado hace la ausencia explícita en vez de ambiental.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


@pytest.fixture
async def dead_redis() -> Iterator[None]:
    """Apunta `core.redis` a un puerto cerrado."""
    from redis.asyncio import Redis

    import core.redis as redis_module

    previous = redis_module._client
    client = Redis.from_url(
        f"redis://127.0.0.1:{closed_port()}/0",
        decode_responses=True,
        socket_timeout=0.25,
        socket_connect_timeout=0.25,
    )
    redis_module._client = client
    try:
        yield
    finally:
        # El test pudo llamar a `close()` y dejar el módulo en None: se cierra
        # el cliente que creó la fixture, no el que haya quedado.
        await client.aclose()
        redis_module._client = previous


@pytest.fixture
async def dead_db() -> Iterator[None]:
    """Apunta `core.db` a un puerto cerrado."""
    from sqlalchemy.ext.asyncio import create_async_engine

    import core.db as db_module

    previous_engine, previous_maker = db_module._engine, db_module._sessionmaker
    engine = create_async_engine(
        f"postgresql+asyncpg://nadie:nadie@127.0.0.1:{closed_port()}/nada",
        connect_args={"timeout": 1},
    )
    db_module._engine = engine
    db_module._sessionmaker = None
    try:
        yield
    finally:
        await engine.dispose()
        db_module._engine, db_module._sessionmaker = previous_engine, previous_maker


@pytest.fixture(scope="session")
def redis_container_url() -> Iterator[str]:
    """Redis real y efímero para los tests que no pueden usar un doble."""
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


@pytest.fixture(scope="session")
def postgres_container_url() -> Iterator[str]:
    """Postgres real y efímero, compartido por toda la sesión de tests.

    El contenedor es caro de levantar (~10s), así que se comparte; el aislado
    entre tests lo da `real_db`, que vacía las tablas.
    """
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16") as container:
        yield (
            f"postgresql+asyncpg://{container.username}:{container.password}"
            f"@{container.get_container_host_ip()}:{container.get_exposed_port(5432)}"
            f"/{container.dbname}"
        )


@pytest.fixture
async def real_db(postgres_container_url: str) -> Iterator[None]:
    """Apunta `core.db` a un Postgres real con el esquema creado y vacío."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import core.db as db_module
    from domain.orm import Base

    engine = create_async_engine(postgres_container_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # TRUNCATE en vez de drop/create: mucho más rápido entre tests y
        # RESTART IDENTITY deja los ids reproducibles.
        tablas = ", ".join(f'"{t}"' for t in Base.metadata.tables)
        await conn.execute(text(f"TRUNCATE {tablas} RESTART IDENTITY CASCADE"))

    previous = (db_module._engine, db_module._sessionmaker)
    db_module._engine = engine
    db_module._sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield
    finally:
        await engine.dispose()
        db_module._engine, db_module._sessionmaker = previous


@pytest.fixture
async def catalogo_cargado(real_db: None) -> None:
    """Base con el catálogo y las tasas del seed, como en producción.

    Depende de `real_db` de forma explícita: sin esa dependencia pytest podría
    ordenarla antes y la carga acabaría contra la base por defecto.
    """
    from cli.seed import DEFAULT_SEEDS_DIR, run_seed
    from cli.tasas import import_csv

    await run_seed()
    await import_csv(DEFAULT_SEEDS_DIR / "tasas.csv")


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
