"""Job `tasas_fetch_dirigido`: el ciclo semanal de tasas, automatizado.

Lunes de madrugada. Recorre las fuentes curadas, lee lo que cada institución
publica, y deja lo que cambió publicado o en la cola de revisión según la
tolerancia. Es el nivel 2 de §15 del foundation.

Doble gate como el resto: `SCHEDULER_TASAS_ENABLED` decide si se registra, y
`tasas_fetch_enabled` del ConfigStore lo apaga en caliente sin reiniciar nada.

**Mientras Chromium no viva en la imagen**, el job del VPS corre sólo las
fuentes que rinden a un cliente HTTP plano (`solo_requieren_js=False`). Las
otras ocho se leen desde la máquina local con `python -m cli tasas fetch
--solo-navegador`, que llama a la misma función. Ver `docs/despliegue.md`.
"""

from __future__ import annotations

from api.services import cache
from core.config_store import effective
from core.logging import get_logger
from llm.providers.base import ErrorPresupuestoAgotado
from rates_agent import pipeline
from scheduler.bitacora import registrar_corrida

log = get_logger(__name__)

JOB_ID = "tasas_fetch_dirigido"


async def tasas_fetch_dirigido() -> None:
    async with registrar_corrida(JOB_ID) as corrida:
        if not effective.tasas_fetch_enabled:
            corrida.omitir("tasas_fetch_enabled=false en ConfigStore")
            log.info("tasas_fetch_omitido")
            return

        try:
            reporte = await pipeline.correr(
                solo_requieren_js=False if effective.tasas_fetch_solo_sin_js else None
            )
        except ErrorPresupuestoAgotado as exc:
            # El techo de gasto no es un fallo: es el límite haciendo su
            # trabajo antes de la primera llamada. Marcarlo FALLIDO haría
            # sonar una alarma por algo que funcionó como debía.
            corrida.omitir(str(exc))
            log.warning("tasas_fetch_sin_presupuesto", error=str(exc))
            return

        corrida.metricas.update(reporte.como_metricas())

        # Sólo si algo llegó a publicarse: la cola de revisión no cambia lo
        # que el comparador sirve, así que invalidar por ella sería tirar el
        # cache cada lunes sin motivo.
        if reporte.publicadas:
            await cache.invalidar()


__all__ = ["JOB_ID", "tasas_fetch_dirigido"]
