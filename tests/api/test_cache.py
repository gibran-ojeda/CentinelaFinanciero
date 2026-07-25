"""Tests del cache del comparador.

Se prueba contra un Redis real: lo que se está verificando —expiración por TTL,
borrado por patrón— es comportamiento del servidor, no de nuestro código.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient

from api.dependencies import ContextoMercado
from api.services import cache
from api.services.comparador import FiltrosComparador, FiltroSeguro, OrdenComparador
from domain.models import ParametrosFiscales, UmbralesBanderas


def _contexto(udi: str = "8.79", inflacion: str = "3.37", retencion: str = "0.90"):
    return ContextoMercado(
        valor_udi=Decimal(udi),
        inflacion_anual=Decimal(inflacion),
        params_fiscales=ParametrosFiscales(
            anio=2026,
            tasa_retencion_capital=Decimal(retencion),
            vigente_desde=date(2026, 1, 1),
        ),
        umbrales=UmbralesBanderas(),
    )


# ─── Construcción de la llave ─────────────────────────────────


def test_same_filters_and_context_produce_the_same_key() -> None:
    filtros = FiltrosComparador(plazo="28")
    assert cache.llave(filtros, _contexto()) == cache.llave(filtros, _contexto())


def test_different_filters_produce_different_keys() -> None:
    contexto = _contexto()
    claves = {
        cache.llave(FiltrosComparador(plazo="28"), contexto),
        cache.llave(FiltrosComparador(plazo="91"), contexto),
        cache.llave(FiltrosComparador(sin_banderas=True), contexto),
        cache.llave(FiltrosComparador(orden=OrdenComparador.GAT), contexto),
        cache.llave(FiltrosComparador(seguro=FiltroSeguro.SOLO_IPAB), contexto),
        cache.llave(FiltrosComparador(monto=Decimal("1000")), contexto),
    }
    assert len(claves) == 6


@pytest.mark.parametrize("cambio", [{"udi": "9.00"}, {"inflacion": "5.00"}, {"retencion": "0.50"}])
def test_a_context_change_produces_a_different_key(cambio: dict[str, str]) -> None:
    """Lo que evita servir números viejos tras un cambio de contexto.

    Si la llave dependiera sólo de los filtros, una UDI o una retención nuevas
    seguirían devolviendo la respuesta anterior hasta que expirara el TTL.
    """
    filtros = FiltrosComparador()
    assert cache.llave(filtros, _contexto()) != cache.llave(filtros, _contexto(**cambio))


def test_keys_share_the_versioned_prefix() -> None:
    """Un cambio en la forma de la respuesta invalida todo subiendo la versión."""
    assert cache.llave(FiltrosComparador(), _contexto()).startswith(cache.PREFIJO)


# ─── Degradación ──────────────────────────────────────────────


@pytest.mark.usefixtures("dead_redis")
async def test_without_redis_everything_degrades_to_no_cache() -> None:
    clave = cache.llave(FiltrosComparador(), _contexto())

    assert await cache.obtener(clave) is None
    assert await cache.guardar(clave, "{}") is False
    assert await cache.invalidar() == 0


# ─── Contra Redis real ────────────────────────────────────────


@pytest.mark.requires_docker
@pytest.mark.usefixtures("real_redis")
class TestConRedisReal:
    async def test_stores_and_retrieves(self) -> None:
        clave = cache.llave(FiltrosComparador(plazo="28"), _contexto())

        assert await cache.guardar(clave, '{"total": 3}') is True
        assert await cache.obtener(clave) == '{"total": 3}'

    async def test_invalidation_clears_every_variant(self) -> None:
        """Escribir una tasa afecta a combinaciones impredecibles de filtros."""
        contexto = _contexto()
        claves = [
            cache.llave(FiltrosComparador(plazo=p), contexto) for p in ("28", "91", "182", "364")
        ]
        for clave in claves:
            await cache.guardar(clave, "{}")

        assert await cache.invalidar() == 4
        for clave in claves:
            assert await cache.obtener(clave) is None

    async def test_invalidation_leaves_other_namespaces_alone(self) -> None:
        """No puede llevarse por delante los locks del scheduler."""
        from core import redis

        await redis.set("brujula:lock:heartbeat", "token")
        await cache.guardar(cache.llave(FiltrosComparador(), _contexto()), "{}")

        await cache.invalidar()

        assert await redis.get("brujula:lock:heartbeat") == "token"

    async def test_invalidating_an_empty_cache_is_harmless(self) -> None:
        assert await cache.invalidar() == 0


# ─── Extremo a extremo por el endpoint ────────────────────────


@pytest.mark.requires_docker
@pytest.mark.usefixtures("comparador_poblado", "real_redis")
class TestEndpoint:
    async def test_second_request_is_served_from_cache(self, api_lectura: AsyncClient) -> None:
        primera = await api_lectura.get("/api/v1/comparador", params={"plazo": "91"})
        segunda = await api_lectura.get("/api/v1/comparador", params={"plazo": "91"})

        assert primera.status_code == segunda.status_code == 200
        # Byte a byte, incluido `generado_en`: es la respuesta guardada.
        assert primera.json() == segunda.json()
        assert primera.json()["generado_en"] == segunda.json()["generado_en"]

    async def test_a_different_filter_is_not_served_from_the_same_entry(
        self, api_lectura: AsyncClient
    ) -> None:
        a_28 = (await api_lectura.get("/api/v1/comparador", params={"plazo": "28"})).json()
        a_91 = (await api_lectura.get("/api/v1/comparador", params={"plazo": "91"})).json()

        assert {f["producto_slug"] for f in a_28["filas"]} != {
            f["producto_slug"] for f in a_91["filas"]
        }

    async def test_invalidation_forces_a_recalculation(self, api_lectura: AsyncClient) -> None:
        primera = (await api_lectura.get("/api/v1/comparador")).json()
        await cache.invalidar()
        segunda = (await api_lectura.get("/api/v1/comparador")).json()

        assert primera["generado_en"] != segunda["generado_en"]
        assert primera["filas"] == segunda["filas"]

    async def test_a_new_rate_is_visible_after_invalidation(
        self, api_lectura: AsyncClient
    ) -> None:
        """El flujo real: se publica una tasa, se invalida, se ve el cambio."""
        from sqlalchemy import select

        from core.db import session_scope
        from domain.enums import EstadoTasa, FuenteTasa
        from domain.orm import Producto, Tasa

        antes = (await api_lectura.get("/api/v1/comparador", params={"plazo": "182"})).json()
        assert "supertasas-plazo-182" not in {f["producto_slug"] for f in antes["filas"]}

        async with session_scope() as session:
            producto_id = await session.scalar(
                select(Producto.id).where(Producto.slug == "supertasas-plazo-182")
            )
            session.add(
                Tasa(
                    producto_id=producto_id,
                    tasa_nominal=Decimal("7.70"),
                    fecha_dato=date(2026, 7, 24),
                    fuente=FuenteTasa.MANUAL,
                    estado=EstadoTasa.VIGENTE,
                )
            )

        # Sin invalidar, el cache sigue sirviendo la vista anterior.
        durante = (await api_lectura.get("/api/v1/comparador", params={"plazo": "182"})).json()
        assert durante["filas"] == antes["filas"]

        await cache.invalidar()
        despues = (await api_lectura.get("/api/v1/comparador", params={"plazo": "182"})).json()
        assert "supertasas-plazo-182" in {f["producto_slug"] for f in despues["filas"]}

    async def test_cached_payload_is_valid_json(self, api_lectura: AsyncClient) -> None:
        await api_lectura.get("/api/v1/comparador")

        from core import redis

        claves = []
        async for clave in redis.get_client().scan_iter(match=cache.PATRON):
            claves.append(clave)

        assert claves
        guardado = await redis.get(claves[0])
        assert guardado is not None
        assert "filas" in json.loads(guardado)
