"""Cliente Redis async con reconexión y degradación.

Regla de diseño: **Redis no es crítico para servir**. Es cache L2 del
comparador, locks del scheduler y cooldowns. Si se cae, la aplicación tiene
que seguir respondiendo — más lenta, no rota. Por eso todas las operaciones de
este módulo capturan el fallo y devuelven un valor neutro (`None`, `False`) en
lugar de propagar.

La única excepción conceptual es el lock distribuido (`scheduler.locks`): ahí un
fallo se traduce en "no se pudo adquirir", que hace que el job **no** corra. Es
la degradación segura: preferimos saltarnos una corrida a ejecutarla dos veces.
"""

from __future__ import annotations

from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from core.logging import get_logger
from core.settings import settings

log = get_logger(__name__)

_client: Redis | None = None


def get_client() -> Redis:
    """Devuelve el cliente del proceso, creándolo en el primer uso.

    `redis-py` gestiona el pool y la reconexión: cada operación toma una
    conexión del pool y reintenta una vez ante error de conexión o timeout.
    """
    global _client
    if _client is None:
        _client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=settings.redis_socket_timeout_seconds,
            socket_connect_timeout=settings.redis_socket_timeout_seconds,
            retry_on_timeout=True,
            health_check_interval=30,
        )
        log.debug("redis_client_created", host=settings.redis_host, port=settings.redis_port)
    return _client


async def ping() -> bool:
    """Ping a Redis. `False` en vez de excepción — lo consume /healthz."""
    try:
        return bool(await get_client().ping())
    except (RedisError, OSError) as exc:
        log.warning("redis_ping_failed", error=str(exc))
        return False


async def get(key: str) -> str | None:
    """Lectura degradable: un Redis caído se comporta como cache miss."""
    try:
        value: str | None = await get_client().get(key)
    except (RedisError, OSError) as exc:
        log.warning("redis_get_failed", key=key, error=str(exc))
        return None
    return value


async def set(key: str, value: str, *, ttl_seconds: int | None = None) -> bool:
    """Escritura degradable. Devuelve si se pudo guardar."""
    try:
        await get_client().set(key, value, ex=ttl_seconds)
    except (RedisError, OSError) as exc:
        log.warning("redis_set_failed", key=key, error=str(exc))
        return False
    return True


async def delete(*keys: str) -> int:
    """Borrado degradable. Devuelve cuántas claves se eliminaron."""
    if not keys:
        return 0
    try:
        removed: int = await get_client().delete(*keys)
    except (RedisError, OSError) as exc:
        log.warning("redis_delete_failed", keys=list(keys), error=str(exc))
        return 0
    return removed


async def delete_pattern(pattern: str) -> int:
    """Invalida por patrón con SCAN (nunca KEYS, que bloquea el servidor).

    Lo usa la invalidación de cache del comparador en la fase 4.
    """
    removed = 0
    try:
        client = get_client()
        batch: list[str] = []
        async for key in client.scan_iter(match=pattern, count=500):
            batch.append(key)
            if len(batch) >= 500:
                removed += await client.delete(*batch)
                batch.clear()
        if batch:
            removed += await client.delete(*batch)
    except (RedisError, OSError) as exc:
        log.warning("redis_delete_pattern_failed", pattern=pattern, error=str(exc))
        return removed
    return removed


async def eval_script(script: str, keys: list[str], args: list[Any]) -> Any:
    """Ejecuta un script Lua. Devuelve `None` si Redis no está disponible."""
    try:
        return await get_client().eval(script, len(keys), *keys, *args)
    except (RedisError, OSError) as exc:
        log.warning("redis_eval_failed", error=str(exc))
        return None


async def close() -> None:
    """Cierra el pool. Se llama en el shutdown de la API y del scheduler."""
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except (RedisError, OSError) as exc:  # noqa: BLE001 — cerrar nunca debe fallar
            log.warning("redis_close_failed", error=str(exc))
        log.debug("redis_client_closed")
    _client = None


__all__ = [
    "close",
    "delete",
    "delete_pattern",
    "eval_script",
    "get",
    "get_client",
    "ping",
    "set",
]
