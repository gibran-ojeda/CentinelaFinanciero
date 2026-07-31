"""Tests de la corrida completa: leer, extraer, decidir.

Lo que se verifica es el orden y sus consecuencias en dinero y en resiliencia:
que una página sin cambios no cueste un token, que una fuente caída no se lleve
a las demás, y que el techo de gasto corte sin marcar la corrida como fallida.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from cli.seed import run_seed
from core.db import session_scope
from domain.enums import EstadoJob, EstadoTasa, FuenteTasa
from domain.orm import FuenteTasas, JobRun, Producto, Tasa
from llm.client import ClienteLLM
from llm.providers.base import ErrorPresupuestoAgotado, ProveedorLLM, RespuestaLLM
from rates_agent import pipeline
from rates_agent.fetcher import ErrorDescarga, Fetcher

pytestmark = [pytest.mark.requires_docker, pytest.mark.usefixtures("real_db", "real_redis")]

PAGINA = """
<html><body><article>
<h1>Inversión a plazo fijo de Finsus</h1>
<p>Estas son nuestras tasas vigentes, calculadas antes de impuestos y sujetas a
cambio sin previo aviso. El monto mínimo de inversión es de cien pesos.</p>
<table><tr><td>364 días</td><td>8.69%</td></tr></table>
</article></body></html>
"""


class TransporteFalso:
    def __init__(self, nombre: str, *guion: str | ErrorDescarga) -> None:
        self.nombre = nombre
        self._guion = list(guion)
        self.llamadas = 0

    async def obtener(self, url: str, *, timeout_s: float) -> str:
        self.llamadas += 1
        siguiente = self._guion.pop(0) if self._guion else PAGINA
        if isinstance(siguiente, ErrorDescarga):
            raise siguiente
        return siguiente

    async def cerrar(self) -> None:
        return None


class ModeloFalso(ProveedorLLM):
    def __init__(self, *, tasas: list[dict] | None = None, sin_presupuesto: bool = False) -> None:
        self.nombre = "doble"
        self.modelo = "doble"
        self.llamadas = 0
        self._tasas = tasas if tasas is not None else []
        self._sin_presupuesto = sin_presupuesto

    async def completar(self, **kwargs: object) -> RespuestaLLM:
        self.llamadas += 1
        if self._sin_presupuesto:
            raise ErrorPresupuestoAgotado("techo diario alcanzado")
        return RespuestaLLM(
            contenido=json.dumps({"tasas": self._tasas}),
            modelo="doble",
            tokens_entrada=1000,
            tokens_salida=100,
            costo_usd=0.0002,
            latencia_ms=1,
        )

    async def ping(self) -> bool:
        return True


def _fetcher(*transportes: TransporteFalso) -> Fetcher:
    return Fetcher(
        list(transportes),  # type: ignore[arg-type]
        respetar_robots=False,
        esperas_backoff_s=(),
        espera_base_s=0.001,
        # El umbral de «página vacía» es del fetcher y lo prueba `test_fetcher`.
        # Aquí sólo hace ruido: `PAGINA` extrae 195 caracteres con trafilatura
        # 2.2 y 201 con 2.1 —la versión nueva ya no dibuja las tablas con
        # barras—, así que contra el valor de producción (200) estos tests
        # dependían de qué versión resolviera pip ese día. Y resolvió otra.
        min_caracteres=1,
    )


async def _solo_una_fuente(url: str = "https://www.finsus.mx/inversion") -> None:
    """Deja una sola fuente activa, para que la corrida sea legible."""
    await run_seed()
    async with session_scope() as session:
        fuentes = (await session.execute(select(FuenteTasas))).scalars().all()
        for fuente in fuentes:
            fuente.activa = fuente.url == url


TASA_364 = {"producto": "Plazo fijo", "tipo": "PLAZO", "plazo_dias": 364, "tasa_nominal": "8.69"}


async def test_a_read_page_becomes_a_queued_review() -> None:
    """Primera lectura oficial: se encola, no se publica."""
    await _solo_una_fuente()
    modelo = ModeloFalso(tasas=[TASA_364])

    reporte = await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", PAGINA)), cliente=ClienteLLM(modelo)
    )

    assert reporte.fuentes == 1
    assert reporte.leidas == 1
    assert reporte.tasas_extraidas == 1
    assert reporte.en_revision == 1
    assert reporte.publicadas == 0


async def test_an_unchanged_page_costs_nothing() -> None:
    """El ahorro que hace viable correr esto cada semana."""
    await _solo_una_fuente()
    modelo = ModeloFalso(tasas=[TASA_364])

    await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", PAGINA)), cliente=ClienteLLM(modelo)
    )
    llamadas_tras_la_primera = modelo.llamadas

    reporte = await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", PAGINA)), cliente=ClienteLLM(modelo)
    )

    assert reporte.sin_cambios_en_la_pagina == 1
    assert modelo.llamadas == llamadas_tras_la_primera  # ni un token más


async def test_the_hash_is_only_stamped_after_a_successful_extraction() -> None:
    """Si la extracción revienta, la próxima corrida tiene que reintentarlo."""
    await _solo_una_fuente()

    await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", PAGINA)),
        cliente=ClienteLLM(ModeloFalso(sin_presupuesto=True)),
    )

    async with session_scope() as session:
        fuente = await session.scalar(
            select(FuenteTasas).where(FuenteTasas.url == "https://www.finsus.mx/inversion")
        )
    assert fuente is not None
    assert fuente.ultimo_hash is None


async def test_a_dead_source_does_not_cost_the_others() -> None:
    await run_seed()
    async with session_scope() as session:
        fuentes = (await session.execute(select(FuenteTasas))).scalars().all()
        activas = {"https://www.finsus.mx/inversion", "https://www.supertasas.com/"}
        for fuente in fuentes:
            fuente.activa = fuente.url in activas

    caida = ErrorDescarga("HTTP 500", transitorio=True)
    modelo = ModeloFalso(tasas=[TASA_364])
    reporte = await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", caida, caida, PAGINA, PAGINA)),
        cliente=ClienteLLM(modelo),
    )

    assert reporte.fuentes == 2
    assert reporte.fallidas == 1
    assert reporte.leidas == 1  # la otra sí se leyó


async def test_a_page_with_no_rates_is_neither_read_nor_failed() -> None:
    await _solo_una_fuente()
    vacia = "<html><body><div id='root'></div></body></html>"
    modelo = ModeloFalso()

    reporte = await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", vacia)), cliente=ClienteLLM(modelo)
    )

    assert reporte.vacias == 1
    assert reporte.fallidas == 0
    assert modelo.llamadas == 0


async def test_an_unknown_tenor_is_a_catalogue_gap() -> None:
    """360 no se encaja en el producto de 364 porque «es casi lo mismo».

    Ese redondeo es exactamente el error que traía el dato del agregador.
    """
    await _solo_una_fuente()
    modelo = ModeloFalso(
        tasas=[
            {"producto": "Plazo 360", "tipo": "PLAZO", "plazo_dias": 360, "tasa_nominal": "8.69"}
        ]
    )

    reporte = await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", PAGINA)), cliente=ClienteLLM(modelo)
    )

    assert reporte.en_revision == 0
    assert len(reporte.huecos_catalogo) == 1
    assert reporte.huecos_catalogo[0]["plazo_dias"] == 360
    assert reporte.huecos_catalogo[0]["institucion"] == "Finsus"


async def test_a_second_reading_within_tolerance_publishes_itself() -> None:
    """El caso frecuente: hay una vigente aprobada y la tasa se movió poco."""
    await _solo_una_fuente()
    async with session_scope() as session:
        producto = await session.scalar(
            select(Producto).where(Producto.slug == "finsus-plazo-364")
        )
        assert producto is not None
        session.add(
            Tasa(
                producto_id=producto.id,
                tasa_nominal=Decimal("8.50"),
                fecha_dato=date.today() - timedelta(days=7),
                fuente=FuenteTasa.FETCH_DIRIGIDO,
                estado=EstadoTasa.VIGENTE,
            )
        )

    reporte = await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", PAGINA)),
        cliente=ClienteLLM(ModeloFalso(tasas=[TASA_364])),
    )

    assert reporte.publicadas == 1
    assert reporte.en_revision == 0


async def test_the_budget_ceiling_stops_the_run_without_failing_it() -> None:
    await _solo_una_fuente()

    reporte = await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", PAGINA)),
        cliente=ClienteLLM(ModeloFalso(sin_presupuesto=True)),
    )

    assert reporte.presupuesto_agotado is True
    assert reporte.fallidas == 0


# ─── El job ───────────────────────────────────────────────────


async def test_the_hot_kill_switch_skips_the_job() -> None:
    from core.config_store import effective, set_value
    from scheduler.jobs.tasas import JOB_ID, tasas_fetch_dirigido

    await run_seed()
    await set_value("tasas_fetch_enabled", "false", actor="test")
    await effective.refresh()
    try:
        await tasas_fetch_dirigido()
    finally:
        await set_value("tasas_fetch_enabled", "true", actor="test")
        await effective.refresh()

    async with session_scope() as session:
        corrida = await session.scalar(
            select(JobRun).where(JobRun.job_id == JOB_ID).order_by(JobRun.id.desc())
        )
    assert corrida is not None
    assert corrida.estado is EstadoJob.OMITIDO
    assert "tasas_fetch_enabled" in str(corrida.metricas)


def test_the_job_is_registered_with_a_lock_long_enough_for_the_backoff() -> None:
    """Si el lock caduca a media corrida, otra instancia empieza encima."""
    from scheduler.jobs.tasas import JOB_ID
    from scheduler.registry import build_registry

    spec = next(job for job in build_registry() if job.id == JOB_ID)

    assert spec.enabled is True
    # El backoff temporal puede sumar 25 minutos por sí solo.
    assert spec.lock_ttl_seconds is not None
    assert spec.lock_ttl_seconds >= 1500


async def test_amount_tiers_of_one_product_are_a_catalogue_gap() -> None:
    """Openbank publica 13% hasta $30 000 y 7% de ahí en adelante.

    Las dos van al mismo producto, y `tasas` tiene clave única
    `(producto, fecha, fuente)`: la segunda chocaría. Elegir una tampoco vale
    — publicar el 13% sería el «hasta 13%» que el extractor tiene prohibido.
    """
    await _solo_una_fuente()
    modelo = ModeloFalso(
        tasas=[
            {"producto": "Tramo 1", "tipo": "PLAZO", "plazo_dias": 364, "tasa_nominal": "13.00"},
            {"producto": "Tramo 2", "tipo": "PLAZO", "plazo_dias": 364, "tasa_nominal": "7.00"},
        ]
    )

    reporte = await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", PAGINA)), cliente=ClienteLLM(modelo)
    )

    assert reporte.publicadas == 0
    assert reporte.en_revision == 0
    assert len(reporte.huecos_catalogo) == 2
    assert {h["tasa_nominal"] for h in reporte.huecos_catalogo} == {"13.00", "7.00"}


async def test_an_unexpected_failure_in_one_source_spares_the_rest() -> None:
    """Una violación de clave única con Openbank se llevó dos fuentes sanas."""
    await run_seed()
    async with session_scope() as session:
        fuentes = (await session.execute(select(FuenteTasas))).scalars().all()
        activas = {"https://www.finsus.mx/inversion", "https://www.supertasas.com/"}
        for fuente in fuentes:
            fuente.activa = fuente.url in activas

    class ModeloQueRevienta(ModeloFalso):
        async def completar(self, **kwargs: object) -> RespuestaLLM:
            self.llamadas += 1
            if self.llamadas == 1:
                raise RuntimeError("algo inesperado")
            return await super().completar(**kwargs)

    modelo = ModeloQueRevienta(tasas=[TASA_364])
    reporte = await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", PAGINA, PAGINA)), cliente=ClienteLLM(modelo)
    )

    assert reporte.fallidas == 1
    assert reporte.leidas == 2  # la segunda fuente sí se procesó
    assert any("RuntimeError" in e for e in reporte.errores)
