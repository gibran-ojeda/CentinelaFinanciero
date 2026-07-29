"""Cliente de alto nivel: el techo de gasto, el reintento y el proveedor juntos.

Es la única puerta por la que el resto del sistema habla con un LLM. Hace tres
cosas que ninguna extracción debería repetir por su cuenta:

1. **Pregunta si queda presupuesto** antes de gastar, y registra el costo
   después. Ver `cost_tracker`.
2. **Reintenta lo transitorio y sólo lo transitorio.** Un 429 o un timeout
   merecen otro intento con espera; un 401 no mejora esperando. Es la misma
   distinción que el fetcher hace entre vacío y error duro, y el backoff es el
   mismo de NarrativeAlpha: exponencial con jitter, `min(base·2ⁿ, tope)` por
   `uniform(0.75, 1.25)`. El jitter importa porque sin él dos extracciones que
   chocan con el mismo 429 vuelven a chocar exactamente a la vez.
3. **Devuelve JSON ya parseado**, con el canal de razonamiento como respaldo.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from core.logging import get_logger
from core.settings import settings
from llm import cost_tracker, parsers
from llm.providers.base import (
    ErrorLimiteDePeticiones,
    ErrorPresupuestoAgotado,
    ErrorProveedor,
    ErrorTiempoAgotado,
    ProveedorLLM,
    RespuestaLLM,
)
from llm.providers.openai_compat import ProveedorOpenAICompat

log = get_logger(__name__)

#: Lo que se reintenta. Todo lo demás se propaga en el primer intento.
TRANSITORIOS = (ErrorLimiteDePeticiones, ErrorTiempoAgotado)


def _espera(intento: int, base: float, tope: float) -> float:
    """`min(base·2ⁿ, tope)` con jitter de ±25 %."""
    return min(base * (2.0**intento), tope) * random.uniform(0.75, 1.25)  # noqa: S311


class ClienteLLM:
    """Un proveedor, con presupuesto y reintentos."""

    def __init__(
        self,
        proveedor: ProveedorLLM | None = None,
        *,
        limite_diario_usd: float | None = None,
        max_reintentos: int = 2,
        espera_base_s: float = 2.0,
        espera_tope_s: float = 60.0,
    ) -> None:
        self._proveedor = proveedor or ProveedorOpenAICompat(
            api_key=settings.deepseek_api_key.get_secret_value(),
            modelo=settings.llm_modelo_extraccion,
            base_url=settings.llm_base_url,
            timeout_s=settings.llm_timeout_seconds,
        )
        self._limite = (
            limite_diario_usd
            if limite_diario_usd is not None
            else settings.llm_cost_daily_limit_usd
        )
        self._max_reintentos = max(0, max_reintentos)
        self._base = espera_base_s
        self._tope = espera_tope_s

    @property
    def modelo(self) -> str:
        return self._proveedor.modelo

    async def completar(
        self,
        *,
        sistema: str,
        usuario: str,
        temperatura: float = 0.0,
        max_tokens: int = 4000,
    ) -> RespuestaLLM:
        """Una llamada, con presupuesto y reintentos. Devuelve la respuesta cruda."""
        if not await cost_tracker.disponible(self._limite):
            raise ErrorPresupuestoAgotado(
                f"techo diario de ${self._limite:.2f} USD alcanzado; la corrida no gasta más"
            )

        ultimo: Exception | None = None
        for intento in range(1 + self._max_reintentos):
            try:
                respuesta = await self._proveedor.completar(
                    sistema=sistema,
                    usuario=usuario,
                    temperatura=temperatura,
                    max_tokens=max_tokens,
                )
            except TRANSITORIOS as exc:
                ultimo = exc
                if intento == self._max_reintentos:
                    break
                # Un 429 con `retry-after` manda: el proveedor sabe mejor que
                # nuestra curva cuánto falta para que vuelva a atender.
                sugerida = getattr(exc, "retry_after", None)
                espera = float(sugerida) if sugerida else _espera(intento, self._base, self._tope)
                log.warning(
                    "llm_reintento",
                    intento=intento + 1,
                    de=self._max_reintentos + 1,
                    espera_s=round(espera, 1),
                    error=str(exc)[:160],
                )
                await asyncio.sleep(espera)
                continue
            await cost_tracker.registrar(respuesta.costo_usd)
            return respuesta

        assert ultimo is not None  # el bucle corre al menos una vez
        raise ultimo

    async def completar_json(
        self,
        *,
        sistema: str,
        usuario: str,
        claves_requeridas: tuple[str, ...] = (),
        max_tokens: int = 4000,
    ) -> tuple[dict[str, Any], RespuestaLLM]:
        """Como `completar`, pero devuelve el objeto JSON ya parseado."""
        respuesta = await self.completar(sistema=sistema, usuario=usuario, max_tokens=max_tokens)
        datos = parsers.parsear_json(
            respuesta.contenido,
            claves_requeridas=claves_requeridas,
            respaldo=respuesta.razonamiento,
        )
        return datos, respuesta

    async def ping(self) -> bool:
        return await self._proveedor.ping()

    async def cerrar(self) -> None:
        await self._proveedor.cerrar()


__all__ = ["TRANSITORIOS", "ClienteLLM", "ErrorPresupuestoAgotado", "ErrorProveedor"]
