"""Bitácora de corridas de job en la tabla `job_runs`.

Es la observabilidad de las ingestas (§13): sin esto, saber si el sync de
Banxico corrió anoche exige leer logs. Con esto es una consulta.

Se usa como context manager alrededor del trabajo real. Registra el inicio,
y al salir cierra la fila con el estado, las métricas y el error si lo hubo::

    async with registrar_corrida("banxico_sync") as corrida:
        corrida.metricas["series_actualizadas"] = 7

Regla dura: **un fallo escribiendo la bitácora no puede tumbar el job**. Si la
base no está, se loguea y el trabajo sigue — al revés sería absurdo, porque la
bitácora existe para observar el trabajo, no para condicionarlo.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from core.db import session_scope
from core.logging import get_logger
from domain.enums import EstadoJob
from domain.orm import JobRun

log = get_logger(__name__)


@dataclass(slots=True)
class Corrida:
    """Handle mutable de la corrida en curso."""

    job_id: str
    metricas: dict[str, Any] = field(default_factory=dict)
    estado: EstadoJob = EstadoJob.EN_CURSO
    run_id: int | None = None

    def omitir(self, motivo: str) -> None:
        """Marca la corrida como no operada (kill-switch, nada que hacer)."""
        self.estado = EstadoJob.OMITIDO
        self.metricas["motivo_omision"] = motivo

    def fallar(self, motivo: str) -> None:
        """Marca la corrida como fallida **sin lanzar**.

        Para el llamador que todavía tiene algo que hacer después del fallo —
        el CLI, que imprime el reporte y elige su código de salida— en vez de
        reventar como hace un job al lanzar.
        """
        self.estado = EstadoJob.FALLIDO
        self.metricas["motivo_fallo"] = motivo


async def _abrir(job_id: str) -> int | None:
    try:
        async with session_scope() as session:
            fila = JobRun(job_id=job_id, estado=EstadoJob.EN_CURSO)
            session.add(fila)
            await session.flush()
            return int(fila.id)
    except Exception as exc:  # noqa: BLE001 — la bitácora nunca bloquea el job
        log.warning("job_run_abrir_failed", job_id=job_id, error=str(exc))
        return None


async def _cerrar(
    run_id: int | None,
    *,
    job_id: str,
    estado: EstadoJob,
    metricas: dict[str, Any],
    error: str | None,
) -> None:
    if run_id is None:
        return
    try:
        async with session_scope() as session:
            fila = await session.get(JobRun, run_id)
            if fila is None:
                return
            fila.fin = datetime.now(UTC)
            fila.estado = estado
            fila.metricas = metricas or None
            fila.error = error
    except Exception as exc:  # noqa: BLE001
        log.warning("job_run_cerrar_failed", job_id=job_id, error=str(exc))


@asynccontextmanager
async def registrar_corrida(job_id: str) -> AsyncIterator[Corrida]:
    """Abre una fila en `job_runs` y la cierra al terminar, pase lo que pase."""
    run_id = await _abrir(job_id)
    corrida = Corrida(job_id=job_id, run_id=run_id)
    error: str | None = None
    try:
        yield corrida
    except Exception as exc:
        corrida.estado = EstadoJob.FALLIDO
        error = f"{type(exc).__name__}: {exc}"
        raise
    else:
        if corrida.estado is EstadoJob.EN_CURSO:
            corrida.estado = EstadoJob.EXITOSO
    finally:
        await _cerrar(
            run_id,
            job_id=job_id,
            estado=corrida.estado,
            metricas=corrida.metricas,
            error=error,
        )


__all__ = ["Corrida", "registrar_corrida"]
