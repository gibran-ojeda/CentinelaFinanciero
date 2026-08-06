"""Registro declarativo de jobs.

`JOBS_REGISTRY` es la única fuente de verdad de qué corre y cuándo. Añadir un
job es añadir una entrada aquí, no tocar el runner.

**Doble gate** (§13 del foundation). Cada job tiene dos interruptores:

1. *Frío*, `enabled` — se resuelve desde `Settings` (env-only) al arrancar y
   decide si el job llega a registrarse. Cambiarlo exige reiniciar.
2. *Caliente*, kill-switch en ConfigStore (fase 2) — hace que el job registrado
   no opere, sin reiniciar nada.

Este módulo implementa el gate frío; el caliente lo consulta cada job.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.settings import settings
from scheduler.jobs import banderas, banxico, cnbv, heartbeat, research, tasas

JobFunc = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class JobSpec:
    """Declaración de un job programado."""

    id: str
    func: JobFunc
    trigger: BaseTrigger
    name: str
    enabled: bool = True
    # Nombre del lock distribuido. Por defecto, el propio id del job.
    lock_name: str | None = None
    # TTL del lock. Debe superar la duración máxima esperada del job.
    lock_ttl_seconds: int | None = None
    # Ventana en la que un disparo ya consumido no se repite. Por defecto se
    # deriva del trigger (ver `runner._derive_cooldown`).
    cooldown_seconds: int | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def lock(self) -> str:
        return self.lock_name or self.id


def build_registry() -> tuple[JobSpec, ...]:
    """Construye el registro leyendo los gates fríos de `Settings`.

    Es una función y no una constante para que los tests puedan reconstruirlo
    con otra configuración sin recargar el módulo.
    """
    return (
        JobSpec(
            id=heartbeat.JOB_ID,
            func=heartbeat.heartbeat,
            trigger=IntervalTrigger(seconds=settings.scheduler_heartbeat_interval_seconds),
            name="Latido del scheduler",
            enabled=settings.scheduler_heartbeat_enabled,
            tags=("infra",),
        ),
        JobSpec(
            id=banderas.JOB_ID,
            func=banderas.banderas_recompute,
            # Diario y de madrugada: los boletines de la CNBV llegan una vez al
            # mes, así que recomputar más seguido sólo serviría para recoger
            # cambios de umbral, y para eso está la ejecución manual.
            trigger=CronTrigger(hour=4, minute=30),
            name="Recomputo de banderas de riesgo",
            enabled=settings.scheduler_banderas_enabled,
            # Recorre todo el catálogo: puede tardar más que el default.
            lock_ttl_seconds=900,
            tags=("dominio",),
        ),
        JobSpec(
            id=banxico.JOB_ID,
            func=banxico.banxico_sync_series,
            # Las siete de la mañana: Banxico publica la UDI de madrugada y la
            # subasta de CETES la tarde del jueves, así que a esta hora ya está
            # todo lo del día. Y va antes de que nadie mire el sitio.
            trigger=CronTrigger(hour=7, minute=0),
            name="Sincronización de series de Banxico",
            enabled=settings.scheduler_banxico_enabled,
            # Nueve series y cuatro productos: la corrida normal dura segundos.
            # El TTL cubre el caso raro de una carga inicial de tres años con
            # el SIE limitando por token.
            lock_ttl_seconds=900,
            tags=("ingesta",),
        ),
        JobSpec(
            id=cnbv.JOB_ID,
            func=cnbv.cnbv_boletines,
            # Diario y no el día 5 del mes: la CNBV publica con uno a tres
            # meses de rezago y sin fecha fija, y preguntar es barato. Ver el
            # docstring de `jobs/cnbv.py` — la ventana de reintento es esto.
            trigger=CronTrigger(hour=5, minute=30),
            name="Ingesta de boletines de la CNBV",
            enabled=settings.scheduler_cnbv_enabled,
            # Descargar dos megas de un portal lento, parsear treinta y tres
            # hojas y recomputar las banderas de todo el catálogo.
            lock_ttl_seconds=1800,
            tags=("ingesta",),
        ),
        JobSpec(
            id=cnbv.JOB_ID_FRESCURA,
            func=cnbv.frescura_check,
            # Después de que hayan corrido las ingestas del día, para que mida
            # el estado de hoy y no el de ayer.
            trigger=CronTrigger(hour=8, minute=0),
            name="Vigilancia de frescura por fuente",
            enabled=settings.scheduler_frescura_enabled,
            tags=("infra",),
        ),
        JobSpec(
            id=tasas.JOB_ID,
            func=tasas.tasas_fetch_rapido,
            # Cada media hora a los minutos 5 y 35: CronTrigger y no
            # IntervalTrigger para que un redeploy no reinicie el reloj, y esos
            # minutos esquivan banderas (04:30), CNBV (05:30), Banxico (07:00)
            # y frescura (08:00) — y también la pasada del navegador (..:20),
            # que no tiene por qué arrancar Chromium mientras ésta corre.
            #
            # Cuarenta y ocho corridas al día no cuestan cuarenta y ocho veces
            # más: el pipeline cortocircuita por hash y una página que no se
            # movió no llama al LLM. Lo que se compra es reaccionar en minutos
            # a un cambio de tasa en vez de en horas.
            trigger=CronTrigger(minute="5,35"),
            name="Lectura de tasas cada 30 minutos (sin navegador)",
            enabled=settings.scheduler_tasas_enabled,
            # Nueve páginas por httpx, sin renderizado: minutos, no horas. El
            # TTL cubre el techo de duración (`tasas_fetch_minutos_max`) con
            # margen y queda por debajo de los 30 minutos entre corridas, así
            # que dos no pueden solaparse.
            lock_ttl_seconds=1500,
            tags=("ingesta",),
        ),
        JobSpec(
            id=tasas.JOB_ID_NAVEGADOR,
            func=tasas.tasas_fetch_navegador,
            # Cada ocho horas al minuto 20, con id y lock propios: comparte el
            # `lock_name` con nadie a propósito. Las dos pasadas leen conjuntos
            # de fuentes disjuntos —`requiere_js` reparte— así que solaparse no
            # corrompería nada; separar los minutos es sólo para no tener
            # Chromium y la pasada barata compitiendo por la RAM del VPS.
            trigger=CronTrigger(hour="*/8", minute=20),
            name="Lectura de tasas con navegador cada 8 horas",
            enabled=settings.scheduler_tasas_enabled,
            # Cuatro páginas, pero con Chromium por medio y reintentos: es la
            # cara de las dos. El TTL tiene que cubrir el peor caso o el lock
            # caduca a media corrida y otra instancia empieza encima.
            lock_ttl_seconds=3600,
            tags=("ingesta",),
        ),
        JobSpec(
            id=research.JOB_ID,
            func=research.tasas_research_abierta,
            # Media hora después de cada fetch dirigido (01:15, 05:15, …): la
            # lista de stale que este job investiga es justo la que el nivel 2
            # acaba de no poder cubrir. Correrlo antes sería buscar en abierto
            # lo que el fetch iba a traer más barato.
            #
            # Que siga la rejilla de 4 horas no multiplica el gasto: quien ya
            # tiene lectura del researcher hoy deja de ser candidata (ver
            # `investigacion._candidatas`), así que en la práctica investiga
            # una vez al día por institución y las otras cinco corridas sólo
            # miran. Lo que se gana es reaccionar en horas, no en días, cuando
            # el fetch pierde una fuente.
            trigger=CronTrigger(hour="1-23/4", minute=15),
            name="Búsqueda abierta de tasas stale",
            enabled=settings.scheduler_research_enabled,
            # Un tool-loop por institución, con varias vueltas al modelo y una
            # búsqueda por vuelta. Es el job más lento del sistema.
            lock_ttl_seconds=3600,
            tags=("ingesta",),
        ),
    )


JOBS_REGISTRY: tuple[JobSpec, ...] = build_registry()


def enabled_jobs(registry: tuple[JobSpec, ...] | None = None) -> tuple[JobSpec, ...]:
    """Los jobs que pasan el gate frío."""
    return tuple(job for job in (registry or JOBS_REGISTRY) if job.enabled)


__all__ = ["JOBS_REGISTRY", "JobSpec", "build_registry", "enabled_jobs"]
