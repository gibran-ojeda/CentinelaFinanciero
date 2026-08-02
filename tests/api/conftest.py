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


async def _sin_verificar(activo: bool) -> None:
    from core.config_store import effective, set_value

    await set_value("mostrar_tasas_sin_verificar", "true" if activo else "false", actor="test")
    await effective.refresh()


@pytest.fixture
async def solo_verificadas(real_db: None) -> AsyncIterator[None]:
    """Catálogo estable: sólo lo verificado.

    Con la bandera encendida —el default— el comparador publica también las
    treinta tasas que el seed dejó en `PENDIENTE_REVISION`. Eso es correcto en
    producto, pero convierte el catálogo en algo que cambia cada vez que se
    añade una tasa al seed, y hay tests que afirman conjuntos exactos porque
    lo que prueban es qué entra y qué sale, no cuántas filas hay.

    Depende de `real_db` de forma explícita porque escribe en la base: sin esa
    dependencia pytest puede ordenarla antes de que el engine apunte al
    contenedor, y la escritura acaba contra la base por defecto.

    Se restaura al salir: `effective` es un singleton de proceso, y dejarlo
    apagado haría que el resto de la sesión dependiera del orden de ejecución.
    """
    await _sin_verificar(False)
    try:
        yield
    finally:
        await _sin_verificar(True)


@pytest.fixture
async def con_no_verificadas(solo_verificadas: None) -> AsyncIterator[None]:
    """Vuelve a encenderla para los tests que prueban la política en sí.

    Se apila sobre `solo_verificadas` en vez de sustituirla para que haya un
    solo punto de restauración.
    """
    await _sin_verificar(True)
    yield


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
