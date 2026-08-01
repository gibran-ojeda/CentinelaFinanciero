"""Cliente HTTP del SIE de Banxico.

Autenticación por el header `Bmx-Token`. La jerarquía de errores hace la misma
distinción que sostiene el fetcher y el proveedor de LLM —lo que el tiempo cura
frente a lo que no—, porque es la que decide si reintentar sirve de algo.

Tres rarezas del SIE que se descubrieron probando contra la API real y que el
código maneja explícitamente, cada una con su test:

1. **Una serie sin datos omite la clave `datos`**, no devuelve una lista vacía.
   Leerla con `serie["datos"]` revienta con `KeyError` justo cuando el sistema
   está sano y la serie simplemente no tuvo publicación ese rango.
2. **La respuesta multi-serie llega desordenada.** Pidiendo 936, 939, 942 y 945
   contestó 936, 945, 942, 939. Se casa por `idSerie` y nunca por posición.
3. **Un token inválido se responde como 400, no como 401**, con el mensaje en el
   cuerpo. Sin mirarlo, ese fallo parecería una petición mal formada y acabaría
   reintentándose para siempre.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import httpx

from core.logging import get_logger
from core.settings import settings

log = get_logger(__name__)

BASE_URL = "https://www.banxico.org.mx/SieAPIRest/service/v1"

#: Cuántas series caben en una petición. El SIE admite bastantes más, pero
#: pedirlas de veinte en veinte mantiene las URLs cortas y los fallos acotados:
#: si un lote falla, se pierde ese lote y no el catálogo entero.
MAX_SERIES_POR_PETICION = 20

#: Códigos que el tiempo puede curar. El 400 no está: el SIE lo usa tanto para
#: el token inválido como para una clave mal formada, y ninguno mejora esperando.
_TRANSITORIOS = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Cómo marca el SIE un hueco en una serie. No es un cero ni un error.
_SIN_DATO = frozenset({"N/E", "n/e", ""})


# ─── Errores ──────────────────────────────────────────────────


class ErrorSIE(Exception):
    """Fallo del SIE que **no** mejora reintentando."""


class ErrorTransitorioSIE(ErrorSIE):
    """Congestión o corte pasajero: reintentar tiene sentido."""

    def __init__(self, mensaje: str = "", retry_after: float | None = None) -> None:
        super().__init__(mensaje)
        self.retry_after = retry_after


class ErrorLimiteSIE(ErrorTransitorioSIE):
    """429. El SIE limita por token."""


class ErrorTokenSIE(ErrorSIE):
    """El token falta, caducó o no es válido. Llega como 400."""


#: Lo que `ClienteSIE` reintenta. Todo lo demás se propaga en el primer intento.
TRANSITORIOS = (ErrorTransitorioSIE,)


# ─── Observaciones ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Observacion:
    """Un punto de una serie: qué día y cuánto."""

    fecha: date
    valor: Decimal


def _parsear_fecha(crudo: str) -> date:
    """`dd/mm/yyyy`, que es como el SIE **responde**.

    Ojo con la asimetría: el endpoint de rango **pide** `yyyy-mm-dd`. Los dos
    formatos conviven en este módulo a propósito, y no es un descuido.
    """
    return datetime.strptime(crudo.strip(), "%d/%m/%Y").date()


def _parsear_valor(crudo: str) -> Decimal | None:
    """El dato, o `None` si el SIE marcó el hueco."""
    limpio = crudo.strip().replace(",", "")
    if limpio in _SIN_DATO:
        return None
    try:
        return Decimal(limpio)
    except InvalidOperation:
        return None


def _espera(intento: int, base: float, tope: float) -> float:
    """`min(base·2ⁿ, tope)` con jitter de ±25 %, como en `llm.client`."""
    return min(base * (2.0**intento), tope) * random.uniform(0.75, 1.25)  # noqa: S311


def _leer_series(payload: dict[str, object]) -> dict[str, list[Observacion]]:
    """Convierte el cuerpo del SIE en `{clave: observaciones}`.

    Las series sin publicación en el rango aparecen en el resultado con lista
    vacía: que una serie no traiga datos es información, y borrarla del mapa
    obligaría a cada llamador a distinguir «no vino» de «no hay».
    """
    bmx = payload.get("bmx")
    if not isinstance(bmx, dict):
        raise ErrorSIE("respuesta sin el objeto 'bmx'")
    series = bmx.get("series")
    if not isinstance(series, list):
        raise ErrorSIE("respuesta sin la lista 'series'")

    resultado: dict[str, list[Observacion]] = {}
    for serie in series:
        if not isinstance(serie, dict):
            continue
        clave = str(serie.get("idSerie", "")).strip()
        if not clave:
            continue
        observaciones: list[Observacion] = []
        # `.get` y no `[...]`: una serie sin publicaciones en el rango llega
        # **sin la clave** `datos`. Verificado contra la API real.
        for punto in serie.get("datos") or []:
            if not isinstance(punto, dict):
                continue
            valor = _parsear_valor(str(punto.get("dato", "")))
            if valor is None:
                log.debug("sie_dato_vacio", serie=clave, fecha=punto.get("fecha"))
                continue
            try:
                observaciones.append(
                    Observacion(fecha=_parsear_fecha(str(punto.get("fecha", ""))), valor=valor)
                )
            except ValueError:
                log.warning("sie_fecha_ilegible", serie=clave, fecha=punto.get("fecha"))
        resultado[clave] = observaciones
    return resultado


# ─── Cliente ──────────────────────────────────────────────────


class ClienteSIE:
    """Un cliente por corrida. Reintenta lo transitorio y sólo lo transitorio."""

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = BASE_URL,
        timeout_s: float | None = None,
        max_reintentos: int = 2,
        espera_base_s: float = 2.0,
        espera_tope_s: float = 60.0,
        cliente: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = (
            token if token is not None else settings.banxico_token.get_secret_value()
        ).strip()
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s if timeout_s is not None else settings.banxico_timeout_seconds
        self._max_reintentos = max(0, max_reintentos)
        self._base = espera_base_s
        self._tope = espera_tope_s
        self._cliente = cliente
        self._propio = cliente is None

    @property
    def hay_token(self) -> bool:
        """Sin token no se llama al SIE: se omite la corrida y se dice por qué."""
        return bool(self._token)

    def _http(self) -> httpx.AsyncClient:
        if self._cliente is None:
            self._cliente = httpx.AsyncClient(
                follow_redirects=True,
                headers={
                    "Bmx-Token": self._token,
                    "Accept": "application/json",
                    # El mismo User-Agent identificado del fetcher: aquí no hace
                    # falta para pasar ningún filtro, pero que Banxico sepa quién
                    # consulta es parte de la misma doctrina.
                    "User-Agent": settings.fetch_user_agent,
                },
            )
        return self._cliente

    async def oportuno(self, claves: list[str]) -> dict[str, list[Observacion]]:
        """El último dato publicado de cada serie."""
        return await self._por_lotes(claves, lambda ruta: f"{ruta}/datos/oportuno")

    async def rango(
        self, claves: list[str], *, desde: date, hasta: date
    ) -> dict[str, list[Observacion]]:
        """Las observaciones entre dos fechas, ambas incluidas.

        El SIE pide las fechas en `yyyy-mm-dd` aunque las devuelva en
        `dd/mm/yyyy`. Un rango invertido o futuro no es un error: contesta 200
        con la serie sin la clave `datos`.
        """
        return await self._por_lotes(
            claves,
            lambda ruta: f"{ruta}/datos/{desde.isoformat()}/{hasta.isoformat()}",
        )

    async def _por_lotes(
        self, claves: list[str], construir: Callable[[str], str]
    ) -> dict[str, list[Observacion]]:
        resultado: dict[str, list[Observacion]] = {}
        for inicio in range(0, len(claves), MAX_SERIES_POR_PETICION):
            lote = claves[inicio : inicio + MAX_SERIES_POR_PETICION]
            ruta = f"{self._base_url}/series/{','.join(lote)}"
            resultado.update(await self._pedir(construir(ruta)))
        # Una serie que el SIE no devolvió en absoluto se refleja como vacía en
        # lugar de faltar: quien llama compara contra lo que pidió, no contra lo
        # que llegó.
        for clave in claves:
            resultado.setdefault(clave, [])
        return resultado

    async def _pedir(self, url: str) -> dict[str, list[Observacion]]:
        if not self.hay_token:
            raise ErrorTokenSIE("BANXICO_TOKEN está vacío")

        ultimo: Exception | None = None
        for intento in range(1 + self._max_reintentos):
            try:
                return _leer_series(await self._una_vez(url))
            except TRANSITORIOS as exc:
                ultimo = exc
                if intento == self._max_reintentos:
                    break
                espera = float(exc.retry_after or 0) or _espera(intento, self._base, self._tope)
                log.warning(
                    "sie_reintento",
                    intento=intento + 1,
                    de=self._max_reintentos + 1,
                    espera_s=round(espera, 1),
                    error=str(exc)[:160],
                )
                await asyncio.sleep(espera)

        assert ultimo is not None  # el bucle corre al menos una vez
        raise ultimo

    async def _una_vez(self, url: str) -> dict[str, object]:
        try:
            resp = await self._http().get(url, timeout=self._timeout_s)
        except httpx.TimeoutException as exc:
            raise ErrorTransitorioSIE(f"timeout tras {self._timeout_s}s") from exc
        except httpx.HTTPError as exc:
            detalle = str(exc).strip() or type(exc).__name__
            raise ErrorTransitorioSIE(f"error de red: {detalle}") from exc

        if resp.status_code >= 400:
            raise self._error_de(resp)

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ErrorSIE("el SIE contestó algo que no es JSON") from exc
        if not isinstance(payload, dict):
            raise ErrorSIE("el SIE contestó un JSON que no es un objeto")
        return payload

    def _error_de(self, resp: httpx.Response) -> ErrorSIE:
        mensaje, detalle = _mensaje_de_error(resp)
        etiqueta = f"HTTP {resp.status_code}: {mensaje}" + (f" ({detalle})" if detalle else "")

        # El token inválido viaja como 400 con el mensaje en el cuerpo. Se
        # reconoce por el texto porque el SIE no da otra señal, y confundirlo
        # con una petición mal formada haría que la corrida reintentara algo
        # que sólo se arregla cambiando el `.env`.
        if resp.status_code == 400 and "token" in mensaje.lower():
            return ErrorTokenSIE(etiqueta)

        if resp.status_code == 429:
            cabecera = resp.headers.get("retry-after")
            return ErrorLimiteSIE(etiqueta, retry_after=_segundos(cabecera))

        if resp.status_code in _TRANSITORIOS:
            return ErrorTransitorioSIE(etiqueta)
        return ErrorSIE(etiqueta)

    async def cerrar(self) -> None:
        if self._cliente is not None and self._propio:
            await self._cliente.aclose()
            self._cliente = None

    async def __aenter__(self) -> ClienteSIE:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.cerrar()


def _mensaje_de_error(resp: httpx.Response) -> tuple[str, str]:
    """`(mensaje, detalle)` del cuerpo de error del SIE, si se puede leer."""
    try:
        cuerpo = resp.json()
    except ValueError:
        return resp.text[:160].strip(), ""
    error = cuerpo.get("error") if isinstance(cuerpo, dict) else None
    if not isinstance(error, dict):
        return resp.text[:160].strip(), ""
    return str(error.get("mensaje", "")).strip(), str(error.get("detalle", "")).strip()


def _segundos(cabecera: str | None) -> float | None:
    if not cabecera:
        return None
    try:
        return float(cabecera)
    except ValueError:
        return None


__all__ = [
    "BASE_URL",
    "MAX_SERIES_POR_PETICION",
    "TRANSITORIOS",
    "ClienteSIE",
    "ErrorLimiteSIE",
    "ErrorSIE",
    "ErrorTokenSIE",
    "ErrorTransitorioSIE",
    "Observacion",
]
