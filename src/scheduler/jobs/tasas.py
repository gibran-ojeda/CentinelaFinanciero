"""El ciclo de tasas, automatizado. **Dos pasadas con cadencias distintas.**

Es el nivel 2 de §15 del foundation: recorrer las fuentes curadas, leer lo que
cada institución publica, y dejar lo que cambió publicado o en la cola de
revisión según la tolerancia.

Van separadas porque cuestan cosas distintas. `tasas_fetch_rapido` es un
cliente HTTP y corre **cada media hora**; `tasas_fetch_navegador` arranca
Chromium —unos 300 MB en un contenedor de 768— y corre **cada ocho**. El
reparto lo decide `requiere_js` de cada fuente, medido el 2026-08-06: nueve
rinden a un cliente plano y cuatro necesitan renderizado. Cada fuente cae en
una sola de las dos, así que no compiten por `ultimo_hash` ni se repiten
trabajo.

La cadencia alta no multiplica el costo: el pipeline cortocircuita por hash de
contenido, así que una página idéntica a la corrida anterior no llama al LLM —
la inmensa mayoría de las corridas del día son gratis. Todas comparten el techo
diario `llm_cost_daily_limit_usd`; agotado, las siguientes quedan OMITIDO, no
FALLIDO.

Doble gate como el resto: `SCHEDULER_TASAS_ENABLED` decide si se registran, y
`tasas_fetch_enabled` del ConfigStore las apaga en caliente sin reiniciar nada.
`tasas_fetch_solo_sin_js` sigue siendo el repliegue del navegador sin deploy,
pero ahora hace lo que su nombre dice: omite la pasada del navegador entera en
vez de armar una cadena que no puede renderizar — ver `docs/despliegue.md`,
«Navegador en el VPS».
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

#: La pasada barata: httpx solo, sobre las fuentes que rinden sin renderizar.
JOB_ID = "tasas_fetch_rapido"
#: La cara: arranca Chromium, y por eso va cada ocho horas y no cada media.
JOB_ID_NAVEGADOR = "tasas_fetch_navegador"


def _armar_fetcher(*, con_navegador: bool) -> Fetcher:
    """La cadena de transportes de cada pasada.

    Cada fuente cae en **una sola** de las dos corridas, según `requiere_js`,
    así que cada cadena lleva un único transporte: no hay nada que encadenar.
    Antes iban los dos juntos y httpx resolvía páginas que necesitaban
    navegador devolviendo el envoltorio de la SPA.

    El navegador se importa aquí dentro: sin el extra `[browser]` instalado,
    importar el módulo no debe tumbar el job barato, que no lo necesita.
    """
    agente = settings.fetch_user_agent
    if not con_navegador:
        transportes: list[Transporte] = [TransporteHttpx(user_agent=agente)]
        return Fetcher(transportes)

    from rates_agent.navegador import TransporteNavegador

    return Fetcher([TransporteNavegador(user_agent=agente)])


async def tasas_fetch_rapido() -> None:
    """Cada media hora, sobre lo que rinde a un cliente HTTP plano."""
    await _correr(JOB_ID, con_navegador=False)


async def tasas_fetch_navegador() -> None:
    """Cada ocho horas, sobre lo que sólo se lee renderizando.

    Aparte y espaciado porque Chromium cuesta unos 300 MB en un contenedor de
    768: leerlas cada media hora junto al resto era lo que había que evitar.
    """
    if effective.tasas_fetch_solo_sin_js:
        # El repliegue sin deploy: en vez de armar una cadena que no puede
        # renderizar —y contar sus fuentes como vacías— esta pasada no corre.
        async with registrar_corrida(JOB_ID_NAVEGADOR) as corrida:
            corrida.omitir("tasas_fetch_solo_sin_js=true en ConfigStore")
            log.info("tasas_fetch_navegador_omitido")
        return
    await _correr(JOB_ID_NAVEGADOR, con_navegador=True)


async def _correr(job_id: str, *, con_navegador: bool) -> None:
    async with registrar_corrida(job_id) as corrida:
        if not effective.tasas_fetch_enabled:
            corrida.omitir("tasas_fetch_enabled=false en ConfigStore")
            log.info("tasas_fetch_omitido", job_id=job_id)
            return

        try:
            # Sólo `fetcher=`, nunca `cliente=` también: `pipeline.correr`
            # cierra lo que recibe únicamente cuando construyó al menos una de
            # las dos piezas (`propios`), y pasarle ambas dejaría Chromium
            # vivo entre corridas de un proceso que no reinicia.
            reporte = await pipeline.correr(
                fetcher=_armar_fetcher(con_navegador=con_navegador),
                solo_requieren_js=con_navegador,
            )
        except ErrorPresupuestoAgotado as exc:
            # El techo de gasto no es un fallo: es el límite haciendo su
            # trabajo antes de la primera llamada. Marcarlo FALLIDO haría
            # sonar una alarma por algo que funcionó como debía.
            corrida.omitir(str(exc))
            log.warning("tasas_fetch_sin_presupuesto", error=str(exc))
            return

        corrida.metricas.update(reporte.como_metricas())

        if reporte.fuentes == 0:
            # Alcanzable desde que las fuentes se apagan solas al acumular
            # fallos: si se pausaran todas, la corrida no tendría nada que
            # hacer y saldría EXITOSA cada media hora. No es un fallo de la
            # corrida —es el catálogo vacío— y por eso avisa en vez de reventar:
            # hacer FALLIDO algo que se apagó por diseño no arregla el catálogo.
            log.error("tasas_sin_fuentes_activas", job_id=job_id)

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
        # cache en cada corrida sin motivo.
        if reporte.publicadas:
            await cache.invalidar()


__all__ = [
    "JOB_ID",
    "JOB_ID_NAVEGADOR",
    "tasas_fetch_navegador",
    "tasas_fetch_rapido",
]
