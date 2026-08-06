"""Contrato del proveedor: jerarquía de errores, respuesta normalizada y ABC.

La jerarquía distingue lo que el llamador puede reintentar de lo que no, que es
la misma distinción que sostiene el fetcher: un 429 o un timeout son transitorios
y merecen otro intento; un 401 no mejora esperando.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

# ─── Errores ──────────────────────────────────────────────────


class ErrorProveedor(Exception):
    """Fallo del proveedor que **no** mejora reintentando (401, 403, 400)."""


class ErrorLimiteDePeticiones(ErrorProveedor):
    """429. Transitorio: el llamador decide si reintenta y cuándo.

    `retry_after` viene de la cabecera homónima cuando el proveedor la manda.
    """

    def __init__(self, mensaje: str = "", retry_after: float | None = None) -> None:
        super().__init__(mensaje)
        self.retry_after = retry_after


class ErrorTiempoAgotado(ErrorProveedor):
    """El proveedor no contestó dentro del timeout. Transitorio."""


class ErrorPresupuestoAgotado(ErrorProveedor):
    """El techo de gasto diario ya se alcanzó. No es un fallo: es el límite."""


class ErrorDeParseo(ErrorProveedor):
    """El modelo contestó algo que no se pudo convertir en lo que se esperaba.

    Conserva `contenido_crudo` para que el reintento pueda mostrarle al modelo
    qué devolvió, y para que quede en el log de una extracción que falló.
    """

    def __init__(self, mensaje: str = "", contenido_crudo: str = "") -> None:
        super().__init__(mensaje)
        self.contenido_crudo = contenido_crudo


class RespuestaTruncada(ErrorDeParseo):
    """El modelo llegó al techo de tokens a media respuesta.

    Hereda de `ErrorDeParseo` porque el efecto es el mismo —lo que llega no se
    puede parsear— pero es una causa distinta y accionable: la página trae más
    de lo que cabe en el techo, no el modelo alucinó. Sin separarlas, el
    mensaje mandaba a buscar una alucinación inexistente, y como el reintento
    del extractor sólo cubría errores de validación, la fuente se truncaba
    igual en cada corrida indefinidamente.
    """


# ─── Respuesta ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LlamadaHerramienta:
    """El modelo pidió ejecutar una herramienta.

    `argumentos` llega ya parseado. Si el modelo mandó un JSON roto —pasa—, el
    proveedor lo deja vacío y anota el crudo en `argumentos_crudos`: el loop
    puede entonces devolverle el error como resultado en vez de reventar.
    """

    id: str
    nombre: str
    argumentos: dict[str, Any] = field(default_factory=dict)
    argumentos_crudos: str = ""


@dataclass(frozen=True, slots=True)
class RespuestaLLM:
    """Respuesta normalizada, independiente del proveedor."""

    contenido: str
    modelo: str
    tokens_entrada: int
    tokens_salida: int
    costo_usd: float
    latencia_ms: int
    finish_reason: str | None = None
    #: Herramientas que el modelo pidió ejecutar. Vacío en todo lo que no sea
    #: el tool-loop del researcher, que es el único que las manda.
    herramientas: tuple[LlamadaHerramienta, ...] = ()
    #: Cadena de razonamiento de los modelos que razonan, **separada** del
    #: contenido. Cuando un razonador deja `contenido` vacío, el JSON final
    #: puede haber quedado aquí — de ahí que se conserve en vez de descartarse.
    razonamiento: str | None = None
    crudo: dict[str, Any] = field(default_factory=dict)

    @property
    def tokens_totales(self) -> int:
        return self.tokens_entrada + self.tokens_salida


# ─── Proveedor ────────────────────────────────────────────────


class ProveedorLLM(ABC):
    """Lo que cualquier proveedor tiene que saber hacer."""

    nombre: str
    modelo: str

    @abstractmethod
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
        """Manda los prompts y devuelve la respuesta normalizada.

        Args:
            mensajes: conversación completa, cuando la hay. La usa el tool-loop
                del researcher, que necesita reenviar lo que el modelo dijo y
                lo que devolvió cada herramienta. Si viene, `sistema` y
                `usuario` se ignoran — es la misma llamada con más historia.
            herramientas: esquemas en el formato de OpenAI. Retirarlas en la
                última ronda es lo que fuerza al modelo a contestar en vez de
                seguir buscando.

        Raises:
            ErrorProveedor: fallo no reintentable (401, 403, petición inválida).
            ErrorLimiteDePeticiones: 429; el llamador decide si reintenta.
            ErrorTiempoAgotado: no contestó a tiempo.
        """
        ...

    @abstractmethod
    async def ping(self) -> bool:
        """¿La llave sirve y el modelo existe? No lanza: devuelve False."""
        ...

    async def cerrar(self) -> None:  # noqa: B027 — opcional a propósito
        """Cierra el cliente HTTP subyacente.

        No es abstracto: un proveedor sin cliente propio no tiene nada que
        cerrar y obligarlo a escribir un `pass` no aporta.
        """

    async def __aenter__(self) -> ProveedorLLM:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.cerrar()


#: Lo que devuelve el proveedor en `finish_reason` cuando cortó por longitud.
FIN_POR_LONGITUD = "length"

__all__ = [
    "FIN_POR_LONGITUD",
    "RespuestaTruncada",
    "ErrorDeParseo",
    "ErrorLimiteDePeticiones",
    "ErrorPresupuestoAgotado",
    "ErrorProveedor",
    "ErrorTiempoAgotado",
    "LlamadaHerramienta",
    "ProveedorLLM",
    "RespuestaLLM",
]
