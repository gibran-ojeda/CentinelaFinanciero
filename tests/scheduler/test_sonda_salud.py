"""Sonda de salud del scheduler.

El healthcheck anterior (`pgrep`) fallaba con 127 en la imagen slim y marcaba
unhealthy un servicio que funcionaba. Este test existe para que la sonda no
vuelva a pasar por buena sin comprobarse contra la base real.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.db import session_scope
from core.settings import settings
from domain.enums import EstadoJob
from domain.orm import JobRun
from scheduler.health import esta_vivo
from scheduler.jobs.heartbeat import JOB_ID, heartbeat

pytestmark = [pytest.mark.requires_docker, pytest.mark.usefixtures("real_db")]


async def _corrida(*, hace_segundos: int, estado: EstadoJob = EstadoJob.EXITOSO) -> None:
    async with session_scope() as session:
        session.add(
            JobRun(
                job_id=JOB_ID,
                inicio=datetime.now(UTC) - timedelta(seconds=hace_segundos),
                estado=estado,
            )
        )


async def test_a_recent_heartbeat_means_alive() -> None:
    await _corrida(hace_segundos=5)
    assert await esta_vivo() is True


async def test_no_run_at_all_means_not_alive() -> None:
    """Un scheduler que nunca ha corrido nada no está sano, está arrancando."""
    assert await esta_vivo() is False


async def test_an_old_heartbeat_means_not_alive() -> None:
    """El proceso puede seguir vivo con el event loop bloqueado.

    Es justamente el caso que `pgrep` no detectaba y que esta sonda sí.
    """
    await _corrida(hace_segundos=settings.scheduler_heartbeat_interval_seconds * 10)
    assert await esta_vivo() is False


async def test_one_missed_beat_is_tolerated() -> None:
    """Margen para un reinicio o una corrida lenta, sin falsos positivos."""
    await _corrida(hace_segundos=int(settings.scheduler_heartbeat_interval_seconds * 1.5))
    assert await esta_vivo() is True


async def test_a_failed_run_does_not_count_as_alive() -> None:
    await _corrida(hace_segundos=5, estado=EstadoJob.FALLIDO)
    assert await esta_vivo() is False


async def test_the_real_heartbeat_makes_the_probe_pass() -> None:
    """De punta a punta: el job escribe, la sonda lo lee."""
    assert await esta_vivo() is False

    await heartbeat()

    assert await esta_vivo() is True


async def test_the_probe_reports_sick_without_a_database(dead_db: None) -> None:
    """Sin base no se puede afirmar que esté sano.

    A diferencia de la API, que degrada sin Redis porque puede seguir
    sirviendo, aquí no hay nada que hacer sin la base: los jobs escriben en
    ella y la bitácora es la única evidencia de que corren.
    """
    assert await esta_vivo() is False
