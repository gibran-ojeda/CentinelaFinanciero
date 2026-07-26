"""Tests de la bitácora de jobs."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from core.db import session_scope
from domain.enums import EstadoJob
from domain.orm import JobRun
from scheduler.bitacora import registrar_corrida
from scheduler.jobs.heartbeat import JOB_ID, heartbeat


async def test_logging_failure_does_not_break_the_job() -> None:
    """Sin base, el trabajo sigue. La bitácora observa, no condiciona."""
    ejecutado = False

    async with registrar_corrida("job-sin-bd") as corrida:
        ejecutado = True
        corrida.metricas["filas"] = 3

    assert ejecutado is True


@pytest.mark.requires_docker
@pytest.mark.usefixtures("real_db")
class TestConBaseReal:
    async def test_successful_run_is_recorded(self) -> None:
        async with registrar_corrida("job-ok") as corrida:
            corrida.metricas["series_actualizadas"] = 7

        async with session_scope() as session:
            fila = await session.scalar(select(JobRun))

        assert fila is not None
        assert fila.job_id == "job-ok"
        assert fila.estado is EstadoJob.EXITOSO
        assert fila.metricas == {"series_actualizadas": 7}
        assert fila.error is None
        assert fila.fin is not None and fila.fin >= fila.inicio

    async def test_failed_run_records_the_error_and_reraises(self) -> None:
        with pytest.raises(ValueError, match="tasa imposible"):
            async with registrar_corrida("job-malo"):
                raise ValueError("tasa imposible")

        async with session_scope() as session:
            fila = await session.scalar(select(JobRun))

        assert fila is not None
        assert fila.estado is EstadoJob.FALLIDO
        assert fila.error is not None and "tasa imposible" in fila.error
        assert fila.fin is not None

    async def test_skipped_run_is_distinguishable_from_success(self) -> None:
        """Un kill-switch apagado no es un éxito ni un fallo."""
        async with registrar_corrida("job-omitido") as corrida:
            corrida.omitir("kill-switch apagado en ConfigStore")

        async with session_scope() as session:
            fila = await session.scalar(select(JobRun))

        assert fila is not None
        assert fila.estado is EstadoJob.OMITIDO
        assert fila.metricas == {"motivo_omision": "kill-switch apagado en ConfigStore"}

    async def test_heartbeat_writes_its_run(self) -> None:
        """Cierra el pendiente que la fase 1 dejó abierto."""
        await heartbeat()

        async with session_scope() as session:
            fila = await session.scalar(select(JobRun).where(JobRun.job_id == JOB_ID))

        assert fila is not None
        assert fila.estado is EstadoJob.EXITOSO
        assert fila.metricas == {"ok": True}

    async def test_each_run_is_a_new_row(self) -> None:
        await heartbeat()
        await heartbeat()

        async with session_scope() as session:
            filas = (await session.execute(select(JobRun))).scalars().all()

        assert len(filas) == 2
