"""Job `tasas_fetch_dirigido`: el ciclo semanal de tasas, automatizado.

Lunes de madrugada. Recorre las fuentes curadas, lee lo que cada institución
publica, y deja lo que cambió publicado o en la cola de revisión según la
tolerancia. Es el nivel 2 de §15 del foundation.

Doble gate como el resto: `SCHEDULER_TASAS_ENABLED` decide si se registra, y
`tasas_fetch_enabled` del ConfigStore lo apaga en caliente sin reiniciar nada.

**Mientras Chromium no viva en la imagen**, el job del VPS corre sólo las
fuentes que rinden a un cliente HTTP plano (`solo_requieren_js=False`). Las
otras once se leen desde la máquina local con `python -m cli tasas fetch
--solo-navegador`, que llama a la misma función. Ver `docs/despliegue.md`.
"""

from __future__ import annotations

from api.services import cache
from core.config_store import effective
from core.logging import get_logger
from core.settings import settings
from llm.providers.base import ErrorPresupuestoAgotado
from rates_agent import pipeline
from rates_agent.fetcher import Fetcher, Transporte, TransporteHttpx
from scheduler.bitacora import registrar_corrida

log = get_logger(__name__)

JOB_ID = "tasas_fetch_dirigido"


def _armar_fetcher() -> Fetcher:
    """La cadena de transportes según el gate caliente.

    httpx siempre; el navegador salvo que `tasas_fetch_solo_sin_js` lo
    excluya — esa llave es el apagado del navegador sin deploy. El ensamblaje
    vive aquí y no en el default de `Fetcher()` a propósito: sin él, apagar
    el gate ensancharía la consulta a las fuentes JS con una cadena que no
    puede renderizarlas, y las once contarían como «vacías» con corrida
    EXITOSA — el fallo silencioso semanal.
    """
    agente = settings.fetch_user_agent
    transportes: list[Transporte] = [TransporteHttpx(user_agent=agente)]
    if not effective.tasas_fetch_solo_sin_js:
        # Import perezoso, espejo de cli/tasas.py: sin el extra [browser]
        # instalado, importar el módulo no debe tumbar el job.
        from rates_agent.navegador import TransporteNavegador

        transportes.append(TransporteNavegador(user_agent=agente))
    return Fetcher(transportes)


async def tasas_fetch_dirigido() -> None:
    async with registrar_corrida(JOB_ID) as corrida:
        if not effective.tasas_fetch_enabled:
            corrida.omitir("tasas_fetch_enabled=false en ConfigStore")
            log.info("tasas_fetch_omitido")
            return

        try:
            # Sólo `fetcher=`, nunca `cliente=` también: `pipeline.correr`
            # cierra lo que recibe únicamente cuando construyó al menos una de
            # las dos piezas (`propios`), y pasarle ambas dejaría Chromium
            # vivo entre corridas de un proceso que no reinicia.
            reporte = await pipeline.correr(
                fetcher=_armar_fetcher(),
                solo_requieren_js=False if effective.tasas_fetch_solo_sin_js else None,
            )
        except ErrorPresupuestoAgotado as exc:
            # El techo de gasto no es un fallo: es el límite haciendo su
            # trabajo antes de la primera llamada. Marcarlo FALLIDO haría
            # sonar una alarma por algo que funcionó como debía.
            corrida.omitir(str(exc))
            log.warning("tasas_fetch_sin_presupuesto", error=str(exc))
            return

        corrida.metricas.update(reporte.como_metricas())

        if reporte.fracaso_total:
            # Sin esto, una llave de DeepSeek vacía producía un EXITOSO con
            # dieciocho fallos idénticos y cero alarma — el peor estado es el
            # que parece sano. Se lanza después del update: la bitácora
            # persiste las métricas en su finally aunque el job reviente.
            raise RuntimeError(
                f"las {reporte.fuentes} fuentes fallaron; primera causa: "
                f"{reporte.errores[0] if reporte.errores else 'desconocida'}"
            )

        # Sólo si algo llegó a publicarse: la cola de revisión no cambia lo
        # que el comparador sirve, así que invalidar por ella sería tirar el
        # cache cada lunes sin motivo.
        if reporte.publicadas:
            await cache.invalidar()


__all__ = ["JOB_ID", "tasas_fetch_dirigido"]
