"""Fixtures de los tests de la API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

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


#: Tasas VIGENTE que el seed real **no** publica, porque están pendientes de
#: verificación. Se añaden para poder probar que cada filtro de §7 excluye lo
#: que debe: con una sola categoría —la gubernamental, que es la única
#: verificada— no habría nada que un filtro pudiera dejar fuera.
#:
#: Se suman a las cinco que el seed sí publica (CETES 28/91/182/364 y BONDDIA),
#: así que el conjunto de prueba mezcla datos reales y sintéticos igual que lo
#: haría el sistema en marcha.
_TASAS_DE_PRUEBA: tuple[tuple[str, str], ...] = (
    ("finsus-plazo-91", "7.50"),  # SOFIPO, PLAZO 91, mín. 100
    ("klar-vista", "8.50"),  # SOFIPO, VISTA, inmediata, mín. 0
    ("nu-cajita-turbo", "13.00"),  # BANCO_DIGITAL, VISTA, mín. 0
    ("nu-plazo-91", "6.70"),  # BANCO_DIGITAL, PLAZO 91
    ("mercado-pago-vista", "12.00"),  # IFPE, VISTA, sin cobertura
    ("libertad-plazo-364", "9.10"),  # SOFIPO, PLAZO 364, mín. 1000
)


@pytest.fixture
async def comparador_poblado(catalogo_cargado: None) -> None:
    """Catálogo con tasas publicables en todas las categorías y plazos."""
    from datetime import date

    from sqlalchemy import select

    from core.db import session_scope
    from domain.enums import EstadoTasa, FuenteTasa
    from domain.orm import Producto, Tasa

    async with session_scope() as session:
        productos = {
            slug: pid
            for slug, pid in (
                (await session.execute(select(Producto.slug, Producto.id))).tuples().all()
            )
        }
        session.add_all(
            Tasa(
                producto_id=productos[slug],
                tasa_nominal=Decimal(tasa),
                fecha_dato=date(2026, 7, 24),
                fuente=FuenteTasa.MANUAL,
                fuente_url="https://example.test/tasas",
                estado=EstadoTasa.VIGENTE,
            )
            for slug, tasa in _TASAS_DE_PRUEBA
        )
