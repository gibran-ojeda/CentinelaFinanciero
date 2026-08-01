"""Tests del reporte de calibración del nivel 3.

Lo que se verifica es la agregación: que las corridas caigan en su semana ISO,
que el gasto diario sume los dos niveles (el techo es compartido) y que la
tasa de aprobación se corte por fuente. Los datos se siembran como los dejan
la bitácora y el reviewer, incluida una corrida sin métricas.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from cli.research import JOB_RESEARCH, reporte
from core.db import session_scope
from domain.enums import EstadoJob, EstadoRevision, EstadoTasa, FuenteTasa
from domain.orm import JobRun, Producto, RevisionTasa, Tasa

pytestmark = [pytest.mark.requires_docker, pytest.mark.usefixtures("real_db")]


def _hace(dias: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=dias)


def _corrida(
    job_id: str = JOB_RESEARCH,
    *,
    hace_dias: int = 1,
    metricas: dict[str, Any] | None = None,
    estado: EstadoJob = EstadoJob.EXITOSO,
) -> JobRun:
    return JobRun(job_id=job_id, inicio=_hace(hace_dias), estado=estado, metricas=metricas)


async def test_weekly_buckets_sum_the_research_metrics() -> None:
    async with session_scope() as session:
        session.add(
            _corrida(
                hace_dias=1,
                metricas={
                    "investigadas": 2,
                    "hallazgos": 3,
                    "publicadas": 1,
                    "en_revision": 2,
                    "busquedas": 4,
                    "tokens": 100,
                    "costo_usd": 0.01,
                },
            )
        )
        session.add(
            _corrida(
                hace_dias=1,
                metricas={"investigadas": 1, "hallazgos": 1, "busquedas": 2, "costo_usd": 0.02},
            )
        )
        # Nueve días de distancia garantizan otra semana ISO.
        session.add(_corrida(hace_dias=10, metricas={"investigadas": 5, "costo_usd": 0.05}))

    salida = await reporte(semanas=4)

    assert len(salida.por_semana) == 2
    reciente = salida.por_semana[sorted(salida.por_semana)[-1]]
    assert reciente.corridas == 2
    assert reciente.investigadas == 3
    assert reciente.hallazgos == 4
    assert reciente.busquedas == 6
    assert reciente.costo_usd == pytest.approx(0.03)


async def test_the_daily_max_sums_l2_and_l3_costs() -> None:
    """El techo es compartido: medir sólo el research subestimaría el riesgo."""
    async with session_scope() as session:
        session.add(_corrida(hace_dias=2, metricas={"costo_usd": 0.30}))
        session.add(_corrida("tasas_fetch_dirigido", hace_dias=2, metricas={"costo_usd": 0.50}))
        session.add(_corrida("tasas_fetch_manual", hace_dias=5, metricas={"costo_usd": 0.10}))

    salida = await reporte(semanas=4)

    assert salida.gasto_max_dia_usd == pytest.approx(0.80)
    assert salida.dia_del_maximo == _hace(2).date().isoformat()


async def test_empty_metrics_omitted_and_degraded_runs_count_without_breaking() -> None:
    """La bitácora deja `metricas=None` cuando no se anotó nada, y DEGRADADA
    vive dentro de las métricas, no en el estado del JobRun."""
    async with session_scope() as session:
        session.add(_corrida(hace_dias=1, metricas=None, estado=EstadoJob.OMITIDO))
        session.add(_corrida(hace_dias=1, metricas={"estado": "DEGRADADA", "costo_usd": 0.001}))
        session.add(_corrida(hace_dias=1, metricas={"presupuesto_agotado": True}))

    salida = await reporte(semanas=4)

    (semana,) = salida.por_semana.values()
    assert semana.corridas == 3
    assert semana.omitidas == 1
    assert semana.degradadas == 1
    assert semana.cortadas_por_presupuesto == 1


@pytest.mark.usefixtures("catalogo_cargado")
async def test_approval_rates_split_by_source() -> None:
    """FETCH_DIRIGIDO es el término de comparación: mismo reviewer, otra vía."""
    async with session_scope() as session:
        productos = (await session.execute(select(Producto.id).limit(3))).scalars().all()

        def _tasa(pid: int, fuente: FuenteTasa, dia: int) -> Tasa:
            return Tasa(
                producto_id=pid,
                tasa_nominal=Decimal("8.00"),
                fecha_dato=date.today() - timedelta(days=dia),
                fuente=fuente,
                estado=EstadoTasa.PENDIENTE_REVISION,
            )

        t1 = _tasa(productos[0], FuenteTasa.LLM_RESEARCH, 1)
        t2 = _tasa(productos[1], FuenteTasa.LLM_RESEARCH, 2)
        t3 = _tasa(productos[2], FuenteTasa.FETCH_DIRIGIDO, 3)
        session.add_all([t1, t2, t3])
        await session.flush()
        session.add_all(
            [
                RevisionTasa(
                    tasa_id=t1.id,
                    motivo="primera lectura",
                    valor_nuevo=Decimal("8.00"),
                    estado=EstadoRevision.APROBADA,
                ),
                RevisionTasa(
                    tasa_id=t2.id,
                    motivo="primera lectura",
                    valor_nuevo=Decimal("8.00"),
                    estado=EstadoRevision.RECHAZADA,
                ),
                RevisionTasa(
                    tasa_id=t3.id,
                    motivo="primera lectura",
                    valor_nuevo=Decimal("8.00"),
                    estado=EstadoRevision.PENDIENTE,
                ),
            ]
        )

    salida = await reporte(semanas=4)

    research = salida.aprobacion[FuenteTasa.LLM_RESEARCH]
    assert (research.aprobadas, research.rechazadas) == (1, 1)
    assert research.porcentaje == pytest.approx(50.0)

    dirigido = salida.aprobacion[FuenteTasa.FETCH_DIRIGIDO]
    assert dirigido.pendientes == 1
    assert dirigido.porcentaje is None  # sin resueltas no hay tasa que afirmar


async def test_render_names_the_bar_and_the_ceiling() -> None:
    """La barra del 80 % y el techo son el criterio: tienen que estar enfrente."""
    async with session_scope() as session:
        session.add(_corrida(hace_dias=1, metricas={"hallazgos": 1, "costo_usd": 0.02}))

    salida = await reporte(semanas=4)
    texto = salida.render()

    assert "80 %" in texto
    assert "$1.00" in texto
    assert "-W" in texto  # etiqueta de semana ISO


async def test_an_empty_window_says_so() -> None:
    salida = await reporte(semanas=4)

    assert "Sin corridas del researcher" in salida.render()
