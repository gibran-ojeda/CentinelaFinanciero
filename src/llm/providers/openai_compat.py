"""Proveedor para cualquier API con el formato de chat completions de OpenAI.

Es el que consume DeepSeek: misma forma de petición y respuesta, `base_url`
distinta. Un solo proveedor cubre todo lo que este proyecto necesita.
"""

from __future__ import annotations

import json
import time
from typing import Any, Literal

import httpx

from core.logging import get_logger
from llm.providers.base import (
    ErrorDeParseo,
    ErrorLimiteDePeticiones,
    ErrorProveedor,
    ErrorTiempoAgotado,
    LlamadaHerramienta,
    ProveedorLLM,
    RespuestaLLM,
)

log = get_logger(__name__)

#: USD por millón de tokens, `(entrada, salida)`. Verificado contra
#: platform.deepseek.com/pricing y contra el uso en producción de
#: NA, que corre estos mismos modelos.
#:
#: **`deepseek-chat` y `deepseek-reasoner` los retiró DeepSeek el 2026-07-24.**
#: Se conservan aquí sólo para que un costo histórico se pueda recalcular; no
#: se deben usar en código nuevo.
PRECIOS: dict[str, tuple[float, float]] = {
    "deepseek-v4-flash": (0.14, 0.28),
    "deepseek-v4-pro": (1.74, 3.48),
    "deepseek-chat": (0.14, 0.28),
    "deepseek-reasoner": (0.55, 2.19),
}

#: Con qué se cobra un modelo que no está en la tabla. No es cero a propósito:
#: un modelo desconocido con precio cero haría invisible su gasto justo cuando
#: el techo diario es la única red que hay.
PRECIO_DESCONOCIDO = (1.00, 3.00)


def costo_usd(modelo: str, tokens_entrada: int, tokens_salida: int) -> float:
    entrada, salida = PRECIOS.get(modelo, PRECIO_DESCONOCIDO)
    return (tokens_entrada * entrada + tokens_salida * salida) / 1_000_000


class ProveedorOpenAICompat(ProveedorLLM):
    """Cliente de `/chat/completions` sobre httpx.

    Se usa httpx directo y no el SDK de `openai` porque lo único que hace falta
    es un POST con JSON: el SDK traería su propio pool, sus propios reintentos y
    su propia jerarquía de errores, y habría que domar las tres para que
    encajaran con las de aquí.
    """

    def __init__(
        self,
        *,
        api_key: str,
        modelo: str,
        base_url: str = "https://api.deepseek.com/v1",
        timeout_s: float = 90.0,
        nombre: str = "deepseek",
    ) -> None:
        self.nombre = nombre
        self.modelo = modelo
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._cliente: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._cliente is None:
            self._cliente = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._timeout_s),
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._cliente

    async def completar(
        self,
        *,
        sistema: str,
        usuario: str,
        temperatura: float = 0.0,
        max_tokens: int = 4000,
        formato: Literal["texto", "json"] = "json",
        mensajes: list[dict[str, Any]] | None = None,
        herramientas: list[dict[str, Any]] | None = None,
    ) -> RespuestaLLM:
        if not self._api_key:
            raise ErrorProveedor(f"{self.nombre}: no hay API key configurada")

        cuerpo: dict[str, Any] = {
            "model": self.modelo,
            "messages": mensajes
            or [
                {"role": "system", "content": sistema},
                {"role": "user", "content": usuario},
            ],
            "temperature": temperatura,
            "max_tokens": max_tokens,
        }
        if herramientas:
            cuerpo["tools"] = herramientas
            cuerpo["tool_choice"] = "auto"
        if formato == "json" and not herramientas:
            # `response_format` y `tools` juntos no se llevan: el modelo tiene
            # que poder contestar con una llamada a herramienta, que no es un
            # objeto JSON del esquema pedido. El JSON se exige en la última
            # ronda, cuando ya se retiraron las herramientas.
            cuerpo["response_format"] = {"type": "json_object"}

        inicio = time.monotonic()
        try:
            resp = await self._http().post("/chat/completions", json=cuerpo)
        except httpx.TimeoutException as exc:
            raise ErrorTiempoAgotado(
                f"{self.nombre}: sin respuesta en {self._timeout_s}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise ErrorProveedor(f"{self.nombre}: error de red — {exc}") from exc
        latencia_ms = int((time.monotonic() - inicio) * 1000)

        if resp.status_code == 429:
            raise ErrorLimiteDePeticiones(
                f"{self.nombre}: 429",
                retry_after=_segundos(resp.headers.get("retry-after")),
            )
        if resp.status_code >= 400:
            raise ErrorProveedor(f"{self.nombre}: HTTP {resp.status_code} — {resp.text[:300]}")

        try:
            datos = resp.json()
            eleccion = datos["choices"][0]
            mensaje = eleccion["message"]
            uso = datos.get("usage") or {}
        except (KeyError, IndexError, ValueError) as exc:
            raise ErrorDeParseo(
                f"{self.nombre}: respuesta con forma inesperada", contenido_crudo=resp.text[:2000]
            ) from exc

        entrada = int(uso.get("prompt_tokens") or 0)
        salida = int(uso.get("completion_tokens") or 0)
        respuesta = RespuestaLLM(
            contenido=(mensaje.get("content") or "").strip(),
            modelo=datos.get("model") or self.modelo,
            tokens_entrada=entrada,
            tokens_salida=salida,
            costo_usd=costo_usd(self.modelo, entrada, salida),
            latencia_ms=latencia_ms,
            finish_reason=eleccion.get("finish_reason"),
            razonamiento=(mensaje.get("reasoning_content") or None),
            herramientas=_llamadas(mensaje.get("tool_calls")),
            crudo=datos,
        )
        log.info(
            "llm_respuesta",
            proveedor=self.nombre,
            modelo=respuesta.modelo,
            tokens=respuesta.tokens_totales,
            costo_usd=round(respuesta.costo_usd, 6),
            latencia_ms=latencia_ms,
            finish_reason=respuesta.finish_reason,
            herramientas=len(respuesta.herramientas) or None,
        )
        return respuesta

    async def ping(self) -> bool:
        try:
            await self.completar(sistema="ping", usuario="ping", max_tokens=1, formato="texto")
        except Exception as exc:  # noqa: BLE001 — el ping reporta, no propaga
            log.warning("llm_ping_fallido", proveedor=self.nombre, error=str(exc)[:200])
            return False
        return True

    async def cerrar(self) -> None:
        if self._cliente is not None:
            await self._cliente.aclose()
            self._cliente = None


def _llamadas(crudas: object) -> tuple[LlamadaHerramienta, ...]:
    """`tool_calls` del mensaje a la forma normalizada.

    Un modelo puede mandar argumentos que no son JSON válido — pasa, y más con
    modelos económicos. No se lanza: se conserva el crudo para que el tool-loop
    le devuelva el error como resultado de la herramienta y le dé otra
    oportunidad, que es más barato que abortar la corrida.
    """
    if not isinstance(crudas, list):
        return ()
    llamadas: list[LlamadaHerramienta] = []
    for cruda in crudas:
        if not isinstance(cruda, dict):
            continue
        funcion = cruda.get("function")
        if not isinstance(funcion, dict):
            continue
        argumentos_crudos = str(funcion.get("arguments") or "")
        try:
            argumentos = json.loads(argumentos_crudos) if argumentos_crudos else {}
        except ValueError:
            argumentos = {}
        llamadas.append(
            LlamadaHerramienta(
                id=str(cruda.get("id") or ""),
                nombre=str(funcion.get("name") or ""),
                argumentos=argumentos if isinstance(argumentos, dict) else {},
                argumentos_crudos=argumentos_crudos,
            )
        )
    return tuple(llamadas)


def _segundos(crudo: str | None) -> float | None:
    try:
        return float(crudo) if crudo else None
    except ValueError:
        return None


__all__ = ["PRECIOS", "PRECIO_DESCONOCIDO", "ProveedorOpenAICompat", "costo_usd"]
