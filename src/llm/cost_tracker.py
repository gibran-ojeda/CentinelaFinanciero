"""Techo de gasto diario en llamadas al LLM (decisión D2: $1 USD/día).

No es un presupuesto operativo. Con ~18 fuentes por corrida —y el
cortocircuito por hash pagando solo las páginas que cambiaron— lo esperado
siguen siendo **centavos**: dos órdenes de magnitud por debajo del techo,
aunque el fetch corra cada 4 horas. El límite existe para que un bucle —un
reintento que no converge, un job que se re-dispara, una página que mete
tokens dinámicos y rompe el hash— no se coma la cuenta antes de que nadie lo
note.

El acumulado vive en Redis con una llave por día y TTL de 48 h, así que rota
solo. Se incrementa con `INCRBYFLOAT`, que es atómico: dos extracciones en
paralelo no se pisan el contador.

**Se pregunta antes de gastar y se registra después.** Entre ambas cosas cabe
un rebase del techo, y es deliberado: partir una llamada a la mitad para
respetar el límite al centavo desperdicia lo ya pagado. El techo se cruza como
mucho por el costo de una llamada.

Si Redis no está disponible, `disponible()` devuelve `True` con un warning: no
se suspende la ingesta porque el contador esté caído — el gasto real de una
corrida es de centavos y el fallo se ve en los logs.
"""

from __future__ import annotations

from datetime import date

from core import redis
from core.logging import get_logger

log = get_logger(__name__)

PREFIJO = "centinela:llm:costo:"
#: Dos días: cubre el cambio de fecha sin dejar llaves para siempre.
TTL_SEGUNDOS = 48 * 3600


def _llave(dia: date | None = None) -> str:
    return f"{PREFIJO}{(dia or date.today()).isoformat()}"


async def gastado_hoy() -> float:
    """USD acumulados hoy. `0.0` si Redis no contesta."""
    crudo = await redis.get(_llave())
    if crudo is None:
        return 0.0
    try:
        return float(crudo)
    except ValueError:
        log.warning("llm_costo_ilegible", valor=crudo[:40])
        return 0.0


async def disponible(limite_usd: float) -> bool:
    """¿Queda presupuesto para otra llamada?

    `True` cuando Redis está caído: el contador es una red de seguridad, no un
    requisito para operar, y detener la ingesta por perderla sería peor.
    """
    if limite_usd <= 0:
        return False
    gastado = await gastado_hoy()
    if gastado >= limite_usd:
        log.warning(
            "llm_presupuesto_agotado",
            gastado_usd=round(gastado, 4),
            limite_usd=limite_usd,
        )
        return False
    return True


async def registrar(costo_usd: float) -> float:
    """Suma el costo de una llamada. Devuelve el acumulado del día."""
    if costo_usd <= 0:
        return await gastado_hoy()
    llave = _llave()
    try:
        total = await redis.get_client().incrbyfloat(llave, costo_usd)
        await redis.get_client().expire(llave, TTL_SEGUNDOS)
    except Exception as exc:  # noqa: BLE001 — contabilizar nunca debe tumbar la ingesta
        log.warning("llm_costo_no_registrado", error=str(exc)[:200], costo_usd=costo_usd)
        return 0.0
    log.info("llm_costo", costo_usd=round(costo_usd, 6), acumulado_usd=round(float(total), 4))
    return float(total)


async def reiniciar(dia: date | None = None) -> None:
    """Borra el acumulado. Para pruebas y para desbloquear a mano una corrida."""
    await redis.delete(_llave(dia))


__all__ = ["PREFIJO", "TTL_SEGUNDOS", "disponible", "gastado_hoy", "registrar", "reiniciar"]
