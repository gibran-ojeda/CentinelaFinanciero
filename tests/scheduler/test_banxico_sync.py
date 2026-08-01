"""Tests del job diario de Banxico: los dos gates y qué queda en la bitácora."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import desc, select

from core.db import session_scope
from domain.enums import EstadoJob
from domain.orm import JobRun, SerieEconomica, ValorSerieEconomica
from ingest_banxico import series as catalogo
from ingest_banxico.client import Observacion
from scheduler.jobs.banxico import JOB_ID, banxico_sync_series

pytestmark = pytest.mark.requires_docker


class ClienteFalso:
    """Cliente del SIE con token y con una respuesta fija."""

    hay_token = True

    def __init__(self, respuestas: dict[str, list[Observacion]] | None = None) -> None:
        self._respuestas = respuestas or {}
        self.cerrado = False

    async def rango(
        self, claves: list[str], *, desde: date, hasta: date
    ) -> dict[str, list[Observacion]]:
        return {clave: list(self._respuestas.get(clave, [])) for clave in claves}

    async def cerrar(self) -> None:
        self.cerrado = True


class SinToken(ClienteFalso):
    hay_token = False


@pytest.fixture
def cliente_falso(monkeypatch: pytest.MonkeyPatch) -> ClienteFalso:
    """Sustituye el `ClienteSIE()` que arma el job."""
    doble = ClienteFalso(
        {
            catalogo.UDI.clave: [Observacion(date(2026, 7, 30), Decimal("8.79"))],
            catalogo.CETES_28.clave: [Observacion(date(2026, 7, 30), Decimal("6.20"))],
        }
    )
    monkeypatch.setattr("scheduler.jobs.banxico.ClienteSIE", lambda *a, **k: doble)
    return doble


async def _ultima_corrida() -> JobRun:
    async with session_scope() as session:
        fila = await session.scalar(
            select(JobRun).where(JobRun.job_id == JOB_ID).order_by(desc(JobRun.inicio)).limit(1)
        )
    assert fila is not None
    return fila


async def _metricas() -> dict[str, Any]:
    return dict((await _ultima_corrida()).metricas or {})


async def test_a_normal_run_stores_series_and_publishes_cetes(
    catalogo_cargado: None, cliente_falso: ClienteFalso
) -> None:
    await banxico_sync_series()

    corrida = await _ultima_corrida()
    assert corrida.estado is EstadoJob.EXITOSO
    metricas = dict(corrida.metricas or {})
    assert metricas["observaciones"] == 2
    assert metricas["tasas_publicadas"] == 1
    assert cliente_falso.cerrado


async def test_the_hot_kill_switch_skips_the_run(
    catalogo_cargado: None, cliente_falso: ClienteFalso
) -> None:
    from core.config_store import effective, set_value

    await set_value("banxico_sync_enabled", "false", motivo="prueba", actor="test")
    await effective.refresh()

    try:
        await banxico_sync_series()
    finally:
        # El snapshot vive en el módulo y sobrevive al truncado de la base: sin
        # restaurarlo, el siguiente test correría con el job apagado.
        await set_value("banxico_sync_enabled", "true", motivo="fin de prueba", actor="test")
        await effective.refresh()

    corrida = await _ultima_corrida()
    assert corrida.estado is EstadoJob.OMITIDO
    assert "banxico_sync_enabled" in (corrida.metricas or {})["motivo_omision"]
    async with session_scope() as session:
        # El seed carga la serie de la UDI; lo que no debe haber es nada nuevo.
        assert (
            await session.scalar(
                select(ValorSerieEconomica.id)
                .join(SerieEconomica, SerieEconomica.id == ValorSerieEconomica.serie_id)
                .where(SerieEconomica.clave_banxico == catalogo.CETES_28.clave)
            )
            is None
        )


async def test_without_a_token_the_run_is_skipped_not_failed(
    catalogo_cargado: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un despliegue puede correr sin `BANXICO_TOKEN` a propósito.

    Marcarlo FALLIDO sonaría una alarma diaria por una decisión de
    configuración, y una alarma diaria que siempre suena enseña a ignorarlas.
    """
    doble = SinToken()
    monkeypatch.setattr("scheduler.jobs.banxico.ClienteSIE", lambda *a, **k: doble)

    await banxico_sync_series()

    corrida = await _ultima_corrida()
    assert corrida.estado is EstadoJob.OMITIDO
    assert "BANXICO_TOKEN" in (corrida.metricas or {})["motivo_omision"]
    assert doble.cerrado


async def test_a_rejected_token_does_fail(
    catalogo_cargado: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Token puesto pero rechazado: hay una credencial que renovar."""
    from ingest_banxico.client import ErrorTokenSIE

    class Rechazado(ClienteFalso):
        async def rango(
            self, claves: list[str], *, desde: date, hasta: date
        ) -> dict[str, list[Observacion]]:
            raise ErrorTokenSIE("HTTP 400: Token inválido")

    doble = Rechazado()
    monkeypatch.setattr("scheduler.jobs.banxico.ClienteSIE", lambda *a, **k: doble)

    with pytest.raises(ErrorTokenSIE):
        await banxico_sync_series()

    corrida = await _ultima_corrida()
    assert corrida.estado is EstadoJob.FALLIDO
    assert corrida.error is not None and "Token inválido" in corrida.error
    assert doble.cerrado


async def test_running_twice_the_same_day_writes_nothing_new(
    catalogo_cargado: None, cliente_falso: ClienteFalso
) -> None:
    await banxico_sync_series()
    await banxico_sync_series()

    metricas = await _metricas()
    assert metricas["observaciones"] == 0
    assert metricas["tasas_publicadas"] == 0
    async with session_scope() as session:
        cuantas = len(
            (
                await session.execute(
                    select(ValorSerieEconomica.id)
                    .join(SerieEconomica, SerieEconomica.id == ValorSerieEconomica.serie_id)
                    .where(SerieEconomica.clave_banxico == catalogo.UDI.clave)
                )
            )
            .scalars()
            .all()
        )
    # Las siete del seed más la que trajo la corrida.
    assert cuantas == 8


def test_the_job_is_registered_with_both_gates() -> None:
    from scheduler.registry import build_registry

    especificacion = next(j for j in build_registry() if j.id == JOB_ID)
    assert especificacion.enabled is True
    assert especificacion.lock == JOB_ID
    assert "ingesta" in especificacion.tags
