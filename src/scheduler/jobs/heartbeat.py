"""Job `heartbeat`: latido del scheduler.

No hace trabajo de dominio. Existe para tres cosas: dar señal de vida en los
logs, servir de banco de pruebas del lock distribuido —es el job con el que se
verifica que dos réplicas no ejecutan lo mismo dos veces— y dejar una fila
periódica en `job_runs` que confirma que la bitácora y la conexión a la base
siguen funcionando.

Esa última función no es decorativa: si el `heartbeat` deja de escribir en
`job_runs`, el problema es de infraestructura, no del job de ingesta que
alguien esté investigando.
"""

from __future__ import annotations

from core.logging import get_logger
from scheduler.bitacora import registrar_corrida

log = get_logger(__name__)

JOB_ID = "heartbeat"


async def heartbeat() -> None:
    async with registrar_corrida(JOB_ID) as corrida:
        corrida.metricas["ok"] = True
        log.info("heartbeat", job_id=JOB_ID)


__all__ = ["JOB_ID", "heartbeat"]
