"""Job `tasas_research_abierta`: el nivel 3, los miércoles.

Dos días después del fetch dirigido, y a propósito: para entonces ya se sabe
qué instituciones quedaron sin dato fresco el lunes, que son exactamente las
que este job investiga. Correrlo antes sería buscar lo que el nivel 2 iba a
traer más barato.

Doble gate como el resto. Y el techo de gasto del `ClienteLLM` es aquí más
importante que en ninguna otra parte: un tool-loop que no converge puede dar
muchas vueltas, y cada vuelta cuesta.
"""

from __future__ import annotations

from api.services import cache
from core.config_store import effective
from core.logging import get_logger
from llm.providers.base import ErrorPresupuestoAgotado
from rates_agent import investigacion
from scheduler.bitacora import registrar_corrida

log = get_logger(__name__)

JOB_ID = "tasas_research_abierta"


async def tasas_research_abierta() -> None:
    async with registrar_corrida(JOB_ID) as corrida:
        if not effective.tasas_research_enabled:
            corrida.omitir("tasas_research_enabled=false en ConfigStore")
            log.info("research_omitido")
            return

        try:
            reporte = await investigacion.correr()
        except ErrorPresupuestoAgotado as exc:
            # El techo hizo su trabajo antes de la primera llamada. No es un
            # fallo: marcarlo FALLIDO haría sonar una alarma por algo que
            # funcionó como debía.
            corrida.omitir(str(exc))
            log.warning("research_sin_presupuesto", error=str(exc))
            return

        corrida.metricas.update(reporte.como_metricas())

        if not reporte.candidatas:
            # Lo bueno: el nivel 2 está cubriendo todo el catálogo.
            corrida.omitir("ninguna institución quedó stale")
            return

        if reporte.degradada:
            # Ningún motor de búsqueda respondió. La corrida terminó sin
            # publicar nada, que es lo correcto, pero hay que enterarse.
            corrida.metricas["estado"] = "DEGRADADA"
            log.warning("research_degradada", candidatas=reporte.candidatas)

        # Sólo si algo llegó a publicarse: la cola de revisión no cambia lo que
        # el comparador sirve.
        if reporte.publicadas:
            await cache.invalidar()


__all__ = ["JOB_ID", "tasas_research_abierta"]
