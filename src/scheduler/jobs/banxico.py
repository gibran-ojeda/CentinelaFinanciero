"""Job `banxico_sync_series`: la ingesta oficial, todas las mañanas.

Trae de Banxico lo que falta y publica las subastas de CETES que aún no estaban
en el comparador. Es el nivel 1 de §15: determinista, sin LLM y sin revisión
humana, porque el número lo firma quien lo emitió.

Diario y no semanal aunque las subastas sean semanales: la UDI se mueve todos
los días y de ella dependen los límites de cobertura en pesos. La petición es
la misma y el sync es incremental, así que la corrida de un día sin novedades
no escribe nada.

Doble gate como el resto: `SCHEDULER_BANXICO_ENABLED` decide si se registra y
`banxico_sync_enabled` del ConfigStore lo apaga en caliente sin reiniciar.
"""

from __future__ import annotations

from api.services import cache
from core.config_store import effective
from core.logging import get_logger
from ingest_banxico import materializer, sync
from ingest_banxico.client import ClienteSIE, ErrorTokenSIE
from scheduler.bitacora import registrar_corrida

log = get_logger(__name__)

JOB_ID = "banxico_sync_series"


async def banxico_sync_series() -> None:
    async with registrar_corrida(JOB_ID) as corrida:
        if not effective.banxico_sync_enabled:
            corrida.omitir("banxico_sync_enabled=false en ConfigStore")
            log.info("banxico_sync_omitido")
            return

        cliente = ClienteSIE()
        if not cliente.hay_token:
            # Sin token no hay nada que intentar. Es OMITIDO y no FALLIDO: el
            # despliegue puede estar corriendo sin `BANXICO_TOKEN` a propósito,
            # y una alarma diaria por una decisión de configuración es ruido
            # que acaba enseñando a ignorar las alarmas.
            corrida.omitir("BANXICO_TOKEN vacío: la ingesta de Banxico no está configurada")
            log.warning("banxico_sin_token")
            await cliente.cerrar()
            return

        try:
            reporte = await sync.sincronizar(cliente=cliente)
        except ErrorTokenSIE as exc:
            # El token existe pero Banxico lo rechaza. Eso sí es un fallo: hay
            # una credencial caducada que alguien tiene que renovar.
            log.error("banxico_token_rechazado", error=str(exc))
            raise
        finally:
            await cliente.cerrar()

        corrida.metricas.update(reporte.como_metricas())

        publicacion = await materializer.materializar()
        corrida.metricas.update(publicacion.como_metricas())

        # Sólo si alguna tasa cambió de verdad. La UDI y el INPC entran por el
        # contexto de cada petición y no por el cache del comparador, así que
        # invalidar por ellos sería tirarlo cada mañana sin motivo.
        if publicacion.publicadas:
            await cache.invalidar()


__all__ = ["JOB_ID", "banxico_sync_series"]
