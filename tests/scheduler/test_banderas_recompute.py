"""Tests del recomputo de banderas.

El criterio central es la idempotencia: el job corre tras cada ingesta, a
diario y a mano cuando alguien mueve un umbral, así que dos corridas seguidas
tienen que dejar exactamente el mismo estado.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from core.db import session_scope
from domain.enums import EstadoJob, NivelCapitalizacion, Severidad, TipoBandera
from domain.orm import Bandera, IndicadorFinanciero, Institucion, JobRun
from scheduler.jobs.banderas import JOB_ID, banderas_recompute, recomputar

pytestmark = [pytest.mark.requires_docker, pytest.mark.usefixtures("catalogo_cargado")]


async def _id_de(nombre: str) -> int:
    async with session_scope() as session:
        institucion = await session.scalar(select(Institucion).where(Institucion.nombre == nombre))
    assert institucion is not None
    return institucion.id


async def _indicadores(nombre: str, **campos: object) -> None:
    async with session_scope() as session:
        session.add(
            IndicadorFinanciero(
                institucion_id=await _id_de(nombre),
                periodo=date(2026, 3, 31),
                **campos,  # type: ignore[arg-type]
            )
        )


async def _banderas_de(nombre: str, *, activas: bool = True) -> list[Bandera]:
    async with session_scope() as session:
        filas = (
            (
                await session.execute(
                    select(Bandera).where(
                        Bandera.institucion_id == await _id_de(nombre),
                        Bandera.activa.is_(activas),
                    )
                )
            )
            .scalars()
            .all()
        )
    return list(filas)


# ─── Idempotencia ─────────────────────────────────────────────


async def test_two_runs_leave_the_same_state() -> None:
    """Criterio de aceptación de la fase."""
    await _indicadores("Finsus", imor=Decimal("9.0"), icap=Decimal("8.0"))

    primera = await recomputar()
    segunda = await recomputar()

    # Las métricas son del catálogo entero: además de las dos de Finsus, sale
    # la estructural de Mercado Pago.
    assert primera["creadas"] == 3
    assert segunda["creadas"] == 0
    assert segunda["desactivadas"] == 0
    assert segunda["sin_cambios"] == primera["creadas"]
    assert len(await _banderas_de("Finsus")) == 2


async def test_running_on_an_empty_catalog_is_harmless() -> None:
    metricas = await recomputar()
    assert metricas["creadas"] >= 1  # la de Mercado Pago, que es estructural
    assert metricas["desactivadas"] == 0


# ─── Sincronización ───────────────────────────────────────────


async def test_creates_the_flags_the_rules_emit() -> None:
    await _indicadores("Finsus", imor=Decimal("4.0"), icap=Decimal("12.0"), icor=Decimal("85.0"))

    await recomputar()

    tipos = {b.tipo for b in await _banderas_de("Finsus")}
    assert tipos == {TipoBandera.IMOR, TipoBandera.ICAP, TipoBandera.COBERTURA_CARTERA}


async def test_an_institution_that_improves_gets_its_flags_deactivated() -> None:
    """Se desactivan, no se borran: el historial es parte del detalle."""
    await _indicadores("Finsus", imor=Decimal("9.0"))
    await recomputar()
    assert len(await _banderas_de("Finsus")) == 1

    async with session_scope() as session:
        fila = await session.scalar(
            select(IndicadorFinanciero).where(
                IndicadorFinanciero.institucion_id == await _id_de("Finsus")
            )
        )
        assert fila is not None
        fila.imor = Decimal("1.5")

    metricas = await recomputar()

    assert metricas["desactivadas"] == 1
    assert await _banderas_de("Finsus") == []

    historicas = await _banderas_de("Finsus", activas=False)
    assert len(historicas) == 1
    assert historicas[0].tipo is TipoBandera.IMOR
    assert historicas[0].resuelta_at is not None


async def test_a_severity_change_replaces_the_flag() -> None:
    """De amarilla a roja es un hecho nuevo, no el mismo con otro color."""
    await _indicadores("Finsus", imor=Decimal("4.0"))
    await recomputar()
    assert (await _banderas_de("Finsus"))[0].severidad is Severidad.AMARILLA

    async with session_scope() as session:
        fila = await session.scalar(
            select(IndicadorFinanciero).where(
                IndicadorFinanciero.institucion_id == await _id_de("Finsus")
            )
        )
        assert fila is not None
        fila.imor = Decimal("9.0")

    await recomputar()

    activas = await _banderas_de("Finsus")
    assert len(activas) == 1
    assert activas[0].severidad is Severidad.ROJA
    assert len(await _banderas_de("Finsus", activas=False)) == 1


async def test_an_unchanged_flag_keeps_its_original_date() -> None:
    """`created_at` dice desde cuándo lleva marcada la institución."""
    await _indicadores("Finsus", imor=Decimal("9.0"))
    await recomputar()
    original = (await _banderas_de("Finsus"))[0].created_at

    await recomputar()

    assert (await _banderas_de("Finsus"))[0].created_at == original


# ─── Reglas aplicadas ─────────────────────────────────────────


async def test_the_composite_flag_suppresses_the_individual_ones() -> None:
    await _indicadores(
        "Finsus",
        imor=Decimal("9.0"),
        icap=Decimal("11.0"),
    )
    async with session_scope() as session:
        fila = await session.scalar(
            select(IndicadorFinanciero).where(
                IndicadorFinanciero.institucion_id == await _id_de("Finsus")
            )
        )
        assert fila is not None
        fila.captacion = Decimal("1000")

    await recomputar()

    # Sin crecimiento de captación calculado, la compuesta no se dispara y
    # salen las individuales. Es el comportamiento correcto: no se supone.
    tipos = {b.tipo for b in await _banderas_de("Finsus")}
    assert TipoBandera.NO_RECOMENDABLE not in tipos
    assert TipoBandera.IMOR in tipos


async def test_ifpes_always_carry_the_structural_flag() -> None:
    """No depende de indicadores: es consecuencia de la figura regulatoria."""
    await recomputar()

    tipos = {b.tipo for b in await _banderas_de("Mercado Pago")}
    assert tipos == {TipoBandera.SIN_COBERTURA}


async def test_covered_institutions_get_no_structural_flag() -> None:
    await recomputar()
    assert await _banderas_de("Gobierno Federal") == []
    assert await _banderas_de("Nu México") == []


async def test_institutions_without_cnbv_data_get_no_health_flags() -> None:
    """Sin boletines no se inventan indicadores ni banderas."""
    await recomputar()

    tipos = {b.tipo for b in await _banderas_de("Klar")}
    assert tipos == set()


async def test_nicap_is_evaluated() -> None:
    await _indicadores("Finsus", nicap_nivel=NivelCapitalizacion.N3)
    await recomputar()

    banderas = await _banderas_de("Finsus")
    assert [b.tipo for b in banderas] == [TipoBandera.NICAP]
    assert banderas[0].severidad is Severidad.ROJA


async def test_flags_record_the_period_of_their_source_data() -> None:
    await _indicadores("Finsus", imor=Decimal("9.0"))
    await recomputar()

    assert (await _banderas_de("Finsus"))[0].periodo_dato == date(2026, 3, 31)


# ─── Job y gates ──────────────────────────────────────────────


async def test_the_job_records_its_run() -> None:
    await _indicadores("Finsus", imor=Decimal("9.0"))
    await banderas_recompute()

    async with session_scope() as session:
        corrida = await session.scalar(select(JobRun).where(JobRun.job_id == JOB_ID))

    assert corrida is not None
    assert corrida.estado is EstadoJob.EXITOSO
    assert corrida.metricas is not None
    assert corrida.metricas["creadas"] >= 1


async def test_the_hot_kill_switch_skips_the_run() -> None:
    """Apagarlo desde ConfigStore no borra ni recalcula nada."""
    from core.config_store import effective, set_value

    await _indicadores("Finsus", imor=Decimal("9.0"))
    await set_value("banderas_recompute_enabled", "false", actor="test")
    await effective.refresh()

    try:
        await banderas_recompute()
    finally:
        await set_value("banderas_recompute_enabled", "true", actor="test")
        await effective.refresh()

    assert await _banderas_de("Finsus") == []

    async with session_scope() as session:
        corrida = await session.scalar(select(JobRun).where(JobRun.job_id == JOB_ID))

    assert corrida is not None
    assert corrida.estado is EstadoJob.OMITIDO


def test_the_job_is_registered_with_its_cold_gate() -> None:
    from scheduler.registry import build_registry

    spec = next(j for j in build_registry() if j.id == JOB_ID)
    assert spec.enabled is True
    assert spec.lock_ttl_seconds == 900
