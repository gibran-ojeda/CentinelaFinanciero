"""Runner del scheduler: registra los jobs y los ejecuta con exclusión.

Cada job del registro se envuelve en `_guarded`, que aplica las dos garantías
de `scheduler.locks` —una corrida por disparo (tick) y sin solapamiento
(lock)— y libera el lock pase lo que pase. Esa envoltura, y no el job, es lo
que se registra en APScheduler: así ningún job puede olvidarse de protegerse.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core import db, redis
from core.logging import get_logger
from core.settings import settings
from scheduler import locks
from scheduler.registry import JobSpec, enabled_jobs

log = get_logger(__name__)

JOB_DEFAULTS = {
    # Una corrida perdida por reinicio se ejecuta si el retraso es < 60s.
    "misfire_grace_time": 60,
    # Varias corridas perdidas colapsan en una sola, no se encolan.
    "coalesce": True,
    # Nunca dos instancias del mismo job en este proceso. Entre procesos lo
    # garantiza el lock de Redis.
    "max_instances": 1,
}


#: Fracción del intervalo que dura la llave de tick. El margen restante deja
#: que caduque antes del siguiente disparo aunque haya deriva entre réplicas.
_COOLDOWN_RATIO = 0.8
#: Ventana para triggers sin espaciado fijo (cron). Las réplicas disparan con
#: milisegundos de diferencia y el siguiente disparo está a horas o días.
_CRON_COOLDOWN_SECONDS = 60


def _derive_cooldown(trigger: BaseTrigger) -> int:
    """Cuánto vive la llave de tick según el tipo de trigger."""
    if isinstance(trigger, IntervalTrigger):
        return max(1, int(trigger.interval.total_seconds() * _COOLDOWN_RATIO))
    return _CRON_COOLDOWN_SECONDS


def _guarded(job: JobSpec) -> Callable[[], Awaitable[None]]:
    """Envuelve el job con tick + lock distribuido y logging de resultado."""
    cooldown = job.cooldown_seconds or _derive_cooldown(job.trigger)

    async def run() -> None:
        # 1) ¿Nos toca este disparo? Sin esto, un job corto se ejecuta en cada
        #    réplica: la primera toma y suelta el lock antes de que la segunda
        #    lo intente.
        if not await locks.claim_tick(job.lock, cooldown_seconds=cooldown):
            log.debug("job_skipped_tick_taken", job_id=job.id)
            return

        # 2) ¿No hay una corrida anterior todavía en vuelo?
        async with locks.job_lock(job.lock, ttl_seconds=job.lock_ttl_seconds) as adquirido:
            if not adquirido:
                # El disparo no llegó a consumirse: se libera para que el
                # siguiente pueda intentarlo en cuanto la corrida larga acabe.
                await locks.release_tick(job.lock)
                log.info("job_skipped_lock_busy", job_id=job.id)
                return
            log.info("job_started", job_id=job.id)
            try:
                await job.func()
            except Exception as exc:
                # Un job que revienta no debe tumbar el scheduler ni impedir
                # que se libere el lock (de eso se encarga el context manager).
                log.exception("job_failed", job_id=job.id, error=str(exc))
            else:
                log.info("job_finished", job_id=job.id)

    run.__name__ = f"guarded_{job.id}"
    return run


def build_scheduler() -> AsyncIOScheduler:
    """Crea el scheduler con los jobs que pasan el gate frío."""
    scheduler = AsyncIOScheduler(job_defaults=JOB_DEFAULTS, timezone=settings.scheduler_timezone)

    jobs = enabled_jobs()
    for job in jobs:
        scheduler.add_job(
            _guarded(job),
            trigger=job.trigger,
            id=job.id,
            name=job.name,
            replace_existing=True,
        )
        log.info("job_registered", job_id=job.id, name=job.name, trigger=str(job.trigger))

    log.info("scheduler_built", jobs=len(jobs), timezone=settings.scheduler_timezone)
    return scheduler


async def run_forever() -> None:
    """Arranca el scheduler y bloquea hasta recibir SIGINT o SIGTERM."""
    scheduler = build_scheduler()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop(sig: signal.Signals) -> None:
        log.info("scheduler_signal", signal=sig.name)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop, sig)
        except NotImplementedError:  # pragma: no cover — Windows
            signal.signal(sig, lambda s, _f: _request_stop(signal.Signals(s)))

    scheduler.start()
    log.info("scheduler_started")
    try:
        await stop.wait()
    finally:
        # wait=True deja terminar los jobs en vuelo antes de cerrar.
        scheduler.shutdown(wait=True)
        await db.dispose_engine()
        await redis.close()
        log.info("scheduler_stopped")


__all__ = ["JOB_DEFAULTS", "build_scheduler", "run_forever"]
