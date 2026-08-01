"""Jobs de la fase 8: los boletines de la CNBV y la vigilancia de frescura.

## Por qué el job de boletines corre a diario y no el día 5

El plan de la fase pedía un cron mensual «con ventana de reintento y cooldown
en Redis», porque la CNBV publica con uno a tres meses de rezago y sin fecha
fija. Se implementa como **un cron diario**, que cumple el mismo requisito con
mucho menos aparato: lo primero que hace `loader.cargar` es preguntar qué es lo
último publicado y compararlo con lo cargado, y si no hay nada nuevo no
descarga un solo byte. La ventana de reintento es entonces «todos los días» y
el cooldown lo da la propia base, que ya sabe qué periodos tiene.

El costo de un día sin novedad son tres consultas al listado del portafolio.
Un cooldown en Redis habría añadido estado que puede desincronizarse de la
verdad —los indicadores cargados— para ahorrar eso.

Doble gate como el resto, y una regla que se hereda de los otros jobs: **que
una fuente cambie de formato no tumba las demás**. Si la CNBV rehace el boletín
de banca, el de SOFIPOs se sigue cargando y la corrida queda FALLIDA con el
detalle de qué se rompió.
"""

from __future__ import annotations

from api.services import cache
from core.config_store import effective
from core.db import session_scope
from core.frescura import evaluar
from core.logging import get_logger
from ingest_cnbv import loader
from scheduler.bitacora import registrar_corrida

log = get_logger(__name__)

JOB_ID = "cnbv_boletines"
JOB_ID_FRESCURA = "frescura_check"


async def cnbv_boletines() -> None:
    async with registrar_corrida(JOB_ID) as corrida:
        if not effective.cnbv_ingesta_enabled:
            corrida.omitir("cnbv_ingesta_enabled=false en ConfigStore")
            log.info("cnbv_ingesta_omitida")
            return

        reporte = await loader.cargar()
        corrida.metricas.update(reporte.como_metricas())

        if reporte.hubo_cambios:
            # Las banderas ya se recomputaron dentro de `cargar`; lo que falta
            # es que el comparador deje de servir las viejas desde el cache.
            await cache.invalidar()

        if reporte.hubo_errores:
            # Un cambio de formato no se traga en silencio: la corrida queda
            # FALLIDA aunque las otras fuentes hayan cargado bien.
            detalles = "; ".join(f.error for f in reporte.fuentes if f.error)
            raise RuntimeError(f"la CNBV cambió el formato de alguna fuente: {detalles}")

        if not reporte.hubo_cambios:
            # Lo normal la mayoría de los días. Se marca OMITIDO para que en
            # `job_runs` se distinga de una corrida que sí trajo algo.
            corrida.omitir("no hay periodos nuevos publicados")


async def frescura_check() -> None:
    """Compara cada fuente contra su SLA y deja el veredicto en `job_runs`.

    No arregla nada ni dispara ninguna ingesta: existe para que «los datos
    llevan tres semanas parados» sea una consulta y no un descubrimiento. Por
    eso una fuente fuera de SLA **no** hace fallar la corrida — el job hizo su
    trabajo: mirar y decirlo.
    """
    async with registrar_corrida(JOB_ID_FRESCURA) as corrida:
        async with session_scope() as session:
            estados = await evaluar(session)

        atrasadas = [e for e in estados if not e.dentro_de_sla]
        corrida.metricas.update(
            {
                "fuentes": {
                    e.fuente.value: {
                        "ultima": (
                            e.ultima_actualizacion.isoformat() if e.ultima_actualizacion else None
                        ),
                        "dias": e.dias,
                        "sla_dias": e.sla_dias,
                        "dentro_de_sla": e.dentro_de_sla,
                        "observaciones": e.observaciones,
                    }
                    for e in estados
                },
                "fuera_de_sla": [e.fuente.value for e in atrasadas],
                "todo_dentro_de_sla": not atrasadas,
            }
        )

        for estado in atrasadas:
            log.warning(
                "frescura_fuera_de_sla",
                fuente=estado.fuente.value,
                dias=estado.dias,
                sla_dias=estado.sla_dias,
                ultima=(
                    estado.ultima_actualizacion.isoformat()
                    if estado.ultima_actualizacion
                    else None
                ),
            )


__all__ = ["JOB_ID", "JOB_ID_FRESCURA", "cnbv_boletines", "frescura_check"]
