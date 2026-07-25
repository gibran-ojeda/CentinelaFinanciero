"""Exclusión distribuida en Redis para jobs del scheduler.

Con varias réplicas del scheduler hay que garantizar **dos** cosas distintas,
y una sola llave no cubre las dos:

1. **Un job no se solapa consigo mismo** — lo da el lock (`acquire`/`release`).
   `max_instances=1` de APScheduler sólo protege dentro de un proceso; el lock
   protege entre procesos.
2. **Un job corre una sola vez por disparo programado** — lo da `claim_tick`.
   El lock por sí solo no basta: un job corto lo toma y lo libera en un
   milisegundo, así que la réplica que dispara 300 ms más tarde lo encuentra
   libre y ejecuta también. El "tick" es una llave con TTL cercano al intervalo
   que **no se libera**: la primera réplica que la pone gana el disparo y las
   demás se saltan la corrida. Expira sola a tiempo para el siguiente disparo.

Detalles del lock que no son opcionales:

1. **TTL obligatorio.** Si el proceso muere con el lock tomado, el TTL lo
   libera. Sin TTL, un crash bloquea el job para siempre.
2. **Liberación compare-and-delete vía Lua.** Un `DELETE` a secas puede borrar
   el lock de *otro* dueño: si el job A se pasa del TTL, el lock expira, B lo
   toma, y entonces A termina y borra el lock de B. El script compara el token
   antes de borrar, de forma atómica.

Degradación: si Redis no está disponible, ni el tick ni el lock se obtienen —
el job **no** corre. Preferimos saltarnos una corrida a ejecutarla dos veces.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.exceptions import RedisError

from core import redis
from core.logging import get_logger
from core.settings import settings

log = get_logger(__name__)

LOCK_PREFIX = "brujula:lock:"
TICK_PREFIX = "brujula:tick:"

# Borra la clave sólo si sigue siendo nuestra. KEYS[1]=clave, ARGV[1]=token.
_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

# Extiende el TTL sólo si el lock sigue siendo nuestro.
_EXTEND_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("expire", KEYS[1], ARGV[2])
else
    return 0
end
"""


def lock_key(name: str) -> str:
    return f"{LOCK_PREFIX}{name}"


def tick_key(name: str) -> str:
    return f"{TICK_PREFIX}{name}"


async def claim_tick(name: str, *, cooldown_seconds: int) -> bool:
    """Reclama el disparo actual del job. Sólo una réplica puede ganarlo.

    A diferencia del lock, esta llave **no se libera**: expira por TTL. El
    `cooldown_seconds` debe ser menor que el intervalo del job (ver
    `runner._derive_cooldown`) para que la llave haya caducado cuando llegue el
    siguiente disparo, pero mayor que la deriva entre réplicas.
    """
    try:
        claimed = await redis.get_client().set(
            tick_key(name), "1", nx=True, ex=max(1, cooldown_seconds)
        )
    except (RedisError, OSError) as exc:
        log.warning("tick_claim_failed", job=name, error=str(exc))
        return False
    return bool(claimed)


async def release_tick(name: str) -> bool:
    """Libera el tick para permitir un reintento inmediato.

    Se usa cuando el job no llegó a ejecutarse (por ejemplo, porque el lock
    estaba ocupado por una corrida anterior que se alargó): en ese caso el
    disparo no se consumió realmente.
    """
    return await redis.delete(tick_key(name)) > 0


async def acquire(name: str, *, ttl_seconds: int | None = None) -> str | None:
    """Intenta tomar el lock. Devuelve el token del dueño, o `None` si no pudo."""
    ttl = ttl_seconds if ttl_seconds is not None else settings.scheduler_lock_ttl_seconds
    token = uuid.uuid4().hex
    try:
        acquired = await redis.get_client().set(lock_key(name), token, nx=True, ex=ttl)
    except (RedisError, OSError) as exc:
        # Sin Redis no hay garantía de exclusión: se salta la corrida.
        log.warning("lock_acquire_failed", lock=name, error=str(exc))
        return None
    if not acquired:
        log.debug("lock_busy", lock=name)
        return None
    log.debug("lock_acquired", lock=name, ttl_seconds=ttl)
    return token


async def release(name: str, token: str) -> bool:
    """Libera el lock sólo si `token` sigue siendo el dueño."""
    result = await redis.eval_script(_RELEASE_SCRIPT, [lock_key(name)], [token])
    released = bool(result)
    if released:
        log.debug("lock_released", lock=name)
    else:
        # El TTL venció y otro dueño tomó el lock mientras corríamos.
        log.warning("lock_release_skipped", lock=name)
    return released


async def extend(name: str, token: str, *, ttl_seconds: int) -> bool:
    """Renueva el TTL si seguimos siendo el dueño. Para jobs largos."""
    result = await redis.eval_script(_EXTEND_SCRIPT, [lock_key(name)], [token, ttl_seconds])
    return bool(result)


@asynccontextmanager
async def job_lock(name: str, *, ttl_seconds: int | None = None) -> AsyncIterator[bool]:
    """Context manager del lock.

    Cede `True` si se obtuvo el lock (y entonces hay que ejecutar el trabajo) o
    `False` si no. La liberación ocurre siempre, incluso si el job lanza::

        async with job_lock("heartbeat") as adquirido:
            if not adquirido:
                return
            ...
    """
    token = await acquire(name, ttl_seconds=ttl_seconds)
    if token is None:
        yield False
        return
    try:
        yield True
    finally:
        await release(name, token)


__all__ = [
    "LOCK_PREFIX",
    "TICK_PREFIX",
    "acquire",
    "claim_tick",
    "extend",
    "job_lock",
    "lock_key",
    "release",
    "release_tick",
    "tick_key",
]
