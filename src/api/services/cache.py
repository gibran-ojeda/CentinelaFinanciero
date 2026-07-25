"""Cache L2 del comparador en Redis.

El comparador es la vista más pedida y la más cara: recorre el catálogo, une
tasas vigentes y banderas, y calcula métricas por fila. Como los datos cambian
en ciclo semanal —o diario, cuando la ingesta de Banxico entre en juego— y las
consultas se repiten muchísimo, cachearla es la optimización obvia.

Dos cuidados que no lo son tanto:

1. **La llave incluye el contexto de cálculo**, no sólo los filtros. Si sólo
   dependiera de los filtros, un cambio en la UDI o en la tasa de retención
   seguiría sirviendo números viejos hasta que expirara el TTL. Con el contexto
   dentro, un cambio de contexto es automáticamente una llave distinta.
2. **La invalidación es por patrón.** Escribir una tasa afecta a un número
   impredecible de combinaciones de filtros, así que se borra el prefijo
   entero en vez de intentar adivinar cuáles.

Si Redis no está, todo esto degrada a no cachear: `core.redis` devuelve
valores neutros y el comparador se calcula como si no existiera cache.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any

from api.dependencies import ContextoMercado
from api.services.comparador import FiltrosComparador
from core import redis
from core.config_store import effective
from core.logging import get_logger

log = get_logger(__name__)

#: Prefijo de todas las llaves del comparador. La versión permite invalidar
#: todo el cache de golpe cambiando este número cuando cambie la forma de la
#: respuesta — un despliegue nuevo no puede servir respuestas con el esquema
#: anterior.
PREFIJO = "centinela:comparador:v1:"
PATRON = f"{PREFIJO}*"


def _serializable(valor: Any) -> Any:
    if hasattr(valor, "value"):
        return valor.value
    return str(valor) if valor is not None else None


def llave(filtros: FiltrosComparador, contexto: ContextoMercado) -> str:
    """Llave determinista para una combinación de filtros y contexto."""
    partes = {
        "filtros": {k: _serializable(v) for k, v in sorted(asdict(filtros).items())},
        "udi": str(contexto.valor_udi),
        "inflacion": str(contexto.inflacion_anual),
        "retencion": str(contexto.params_fiscales.tasa_retencion_capital),
    }
    huella = hashlib.sha256(
        json.dumps(partes, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:24]
    return f"{PREFIJO}{huella}"


async def obtener(clave: str) -> str | None:
    return await redis.get(clave)


async def guardar(clave: str, payload: str) -> bool:
    """Guarda con el TTL del ConfigStore, ajustable sin desplegar."""
    return await redis.set(clave, payload, ttl_seconds=effective.cache_comparador_ttl_seconds)


async def invalidar() -> int:
    """Borra todo el cache del comparador. Devuelve cuántas llaves cayeron.

    Se llama al escribir tasas o banderas. Es deliberadamente amplio: acertar
    qué combinaciones de filtros se ven afectadas por una tasa nueva es más
    caro y más frágil que recalcularlas.
    """
    borradas = await redis.delete_pattern(PATRON)
    if borradas:
        log.info("cache_comparador_invalidado", llaves=borradas)
    return borradas


__all__ = ["PATRON", "PREFIJO", "guardar", "invalidar", "llave", "obtener"]
