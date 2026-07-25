"""Job `heartbeat`: latido del scheduler.

No hace trabajo de dominio. Existe para dos cosas: dar señal de vida en los
logs y servir de banco de pruebas del lock distribuido — es el job con el que
se verifica que dos réplicas del scheduler no ejecutan lo mismo dos veces.

En la fase 2 escribirá su corrida en la tabla `job_runs`.
"""

from __future__ import annotations

from core.logging import get_logger

log = get_logger(__name__)

JOB_ID = "heartbeat"


async def heartbeat() -> None:
    log.info("heartbeat", job_id=JOB_ID)


__all__ = ["JOB_ID", "heartbeat"]
