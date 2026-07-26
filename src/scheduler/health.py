"""Sonda de salud del scheduler: `python -m scheduler.health`.

El healthcheck original era `pgrep -f 'python -m scheduler'`, y tenía dos
problemas. El primero es que `pgrep` no existe en `python:3.12-slim` —viene en
`procps`, que no se instala— así que el contenedor se marcaba **unhealthy con
el servicio funcionando perfectamente**, y en producción eso significa que el
orquestador lo reinicia en bucle.

El segundo es más de fondo: que el proceso exista no dice que esté planificando
nada. Un `AsyncIOScheduler` con el event loop bloqueado sigue siendo un proceso
vivo. La pregunta que importa es «¿ha corrido algo hace poco?», y eso ya está
escrito en `job_runs` por el heartbeat, que existe precisamente para ser la
prueba de vida del servicio.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import desc, select

from core.db import session_scope
from core.settings import settings
from domain.enums import EstadoJob
from domain.orm import JobRun
from scheduler.jobs.heartbeat import JOB_ID

#: Cuántos intervalos de heartbeat puede fallar antes de declararse enfermo.
#: Tres deja margen para un reinicio o una corrida lenta sin dar un falso
#: positivo, y sigue detectando un servicio parado en menos de cinco minutos.
_INTERVALOS_DE_GRACIA = 3


async def esta_vivo() -> bool:
    """¿Corrió el heartbeat dentro de la ventana de gracia?"""
    limite = datetime.now(UTC) - timedelta(
        seconds=settings.scheduler_heartbeat_interval_seconds * _INTERVALOS_DE_GRACIA
    )

    try:
        async with session_scope() as session:
            corrida = await session.scalar(
                select(JobRun)
                .where(JobRun.job_id == JOB_ID, JobRun.estado == EstadoJob.EXITOSO)
                .order_by(desc(JobRun.inicio))
                # `ix_job_runs_job_inicio` cubre exactamente este filtro y
                # este orden: la sonda corre cada 30s y no debe pasear la tabla.
                .limit(1)
            )
    except Exception:
        # Sin base no se puede afirmar que esté sano. La API distingue entre
        # dependencia crítica y opcional; aquí no hay nada que servir sin ella.
        return False

    if corrida is None:
        return False

    iniciado = corrida.inicio
    if iniciado.tzinfo is None:
        iniciado = iniciado.replace(tzinfo=UTC)
    return iniciado >= limite


def main() -> int:
    return 0 if asyncio.run(esta_vivo()) else 1


if __name__ == "__main__":
    sys.exit(main())
