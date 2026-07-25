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
from apscheduler.triggers.interval import IntervalTrigger

from core.settings import settings
from scheduler.jobs import heartbeat

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
    )


JOBS_REGISTRY: tuple[JobSpec, ...] = build_registry()


def enabled_jobs(registry: tuple[JobSpec, ...] | None = None) -> tuple[JobSpec, ...]:
    """Los jobs que pasan el gate frío."""
    return tuple(job for job in (registry or JOBS_REGISTRY) if job.enabled)


__all__ = ["JOBS_REGISTRY", "JobSpec", "build_registry", "enabled_jobs"]
