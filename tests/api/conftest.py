"""Fixtures de los tests de la API."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from core.settings import settings

# Se leen de la configuración en vez de repetir el literal: los tests usan
# exactamente las llaves con las que está configurada la app.
READ_KEY = settings.api_read_key.get_secret_value()
ADMIN_KEY = settings.api_admin_key.get_secret_value()


@pytest.fixture
def read_key() -> str:
    return READ_KEY


@pytest.fixture
def admin_key() -> str:
    return ADMIN_KEY


@pytest.fixture
async def api() -> AsyncIterator[AsyncClient]:
    """Cliente sin credenciales. Cada test decide qué llave manda."""
    from api.app import create_app

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def api_lectura(api: AsyncClient) -> AsyncClient:
    """Cliente con la llave del BFF: puede leer, no escribir."""
    api.headers["X-API-Key"] = READ_KEY
    return api


@pytest.fixture
async def api_admin(api: AsyncClient) -> AsyncClient:
    api.headers["X-API-Key"] = ADMIN_KEY
    return api


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
