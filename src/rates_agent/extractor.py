"""Del texto de una página a tasas estructuradas. Es la única parte con LLM.

El reparto de §15 del foundation: la parte frágil —conseguir la página— es
determinista y vive en `fetcher`; la parte cambiante —dónde está la tabla esta
semana— la absorbe el modelo. Un selector CSS sobre estas páginas se rompe con
cada rediseño y en silencio, que es la peor forma de romperse.

Lo que este módulo **no** hace: decidir. Devuelve lo que la página dice, con una
confianza declarada. Publicar o no publicar es del `reviewer`, y es
determinista.

La validación es estricta a propósito. Un modelo que devuelve `plazo_dias: 0`
para una cuenta a la vista, o una tasa de 950 %, es un modelo que se equivocó, y
dejar pasar eso convierte un error visible en un dato publicado.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.logging import get_logger
from domain.enums import TipoProducto
from llm.client import ClienteLLM
from llm.providers.base import ErrorDeParseo, RespuestaTruncada
from rates_agent import prompts

log = get_logger(__name__)

#: Por encima de esto es un error de captura o de lectura, no una oferta. El
#: mismo umbral que usa el alta manual en `cli.tasas`.
TASA_MAXIMA_PLAUSIBLE = Decimal("100")

#: Techo de salida para la extracción. Se declara aquí y no se hereda del
#: default del proveedor porque es una decisión de **esta** tarea: una tabla
#: con quince plazos, su GAT nominal y su GAT real ocupa bastante más JSON que
#: una respuesta cualquiera. Si aun así se corta, se reintenta con el doble.
MAX_TOKENS = 6000


class TasaExtraida(BaseModel):
    """Una tasa tal como la publica la institución, sin interpretar."""

    model_config = ConfigDict(str_strip_whitespace=True)

    producto: str = Field(min_length=1, max_length=200)
    tipo: TipoProducto
    plazo_dias: int | None = None
    tasa_nominal: Decimal
    gat_nominal: Decimal | None = None
    gat_real: Decimal | None = None
    monto_minimo: Decimal | None = None
    monto_maximo: Decimal | None = None
    """Donde **acaba** el tramo, si la página lo dice.

    Es transcripción, no juicio: «15% en tus primeros $25,000» son
    `monto_minimo=0` y `monto_maximo=25000`. Sin este campo, una página que
    anuncia un tope y calla por encima era inexpresable —`reconstruir_escalera`
    deducía cada techo del piso del tramo siguiente, así que el último siempre
    quedaba en infinito— y el tope desaparecía en silencio.
    """

    condiciones: str | None = None
    confianza: Literal["alta", "media", "baja"] = "media"

    @field_validator("tasa_nominal", "gat_nominal")
    @classmethod
    def _plausible(cls, valor: Decimal | None) -> Decimal | None:
        if valor is None:
            return None
        if not (Decimal("0") <= valor <= TASA_MAXIMA_PLAUSIBLE):
            raise ValueError(f"tasa fuera de rango plausible: {valor}")
        return valor

    @field_validator("gat_real")
    @classmethod
    def _gat_real_puede_ser_negativa(cls, valor: Decimal | None) -> Decimal | None:
        # La GAT real sí puede ser negativa: es lo que pasa cuando el
        # rendimiento no alcanza a la inflación, y es justo el número que este
        # proyecto existe para enseñar.
        if valor is not None and not (Decimal("-100") <= valor <= TASA_MAXIMA_PLAUSIBLE):
            raise ValueError(f"GAT real fuera de rango plausible: {valor}")
        return valor

    @model_validator(mode="after")
    def _plazo_coherente_con_tipo(self) -> TasaExtraida:
        if self.tipo is TipoProducto.PLAZO and not self.plazo_dias:
            raise ValueError("un producto a PLAZO necesita plazo_dias")
        if self.tipo is TipoProducto.VISTA and self.plazo_dias:
            raise ValueError("un producto a la VISTA no lleva plazo_dias")
        if self.plazo_dias is not None and not (1 <= self.plazo_dias <= 3650):
            raise ValueError(f"plazo fuera de rango: {self.plazo_dias}")
        return self

    @model_validator(mode="after")
    def _tramo_con_recorrido(self) -> TasaExtraida:
        # Se rechaza aquí y no en `validar_escalera` porque allí sólo se
        # comprueba el techo del último tramo: un `monto_maximo` absurdo en
        # una entrada intermedia se perdería, y esto además da un error que
        # nombra la entrada culpable.
        if self.monto_maximo is None:
            return self
        piso = self.monto_minimo or Decimal("0")
        if self.monto_maximo <= piso:
            raise ValueError(
                f"tramo sin recorrido: acaba en {self.monto_maximo} y empieza en {piso}"
            )
        return self


#: Cuántos reclamos ambiguos se guardan y cuánto de cada uno. Es material para
#: una bandera, no un archivo: con el titular de la página basta para decir que
#: no dice a qué corresponde su número.
MAX_AMBIGUAS = 5
MAX_AMBIGUA_CARACTERES = 300


class Extraccion(BaseModel):
    """Lo que el modelo devolvió para una página."""

    tasas: list[TasaExtraida] = Field(default_factory=list)

    ambiguas: list[str] = Field(default_factory=list)
    """Los reclamos con pinta de tasa que la regla 1 obligó a descartar.

    El modelo ya tomaba esa decisión y se la tragaba, así que una página que
    sólo anuncia «hasta 12 % anual» era indistinguible de una que no habla de
    tasas: las dos volvían con `tasas: []`. Son cosas muy distintas para quien
    compara, y de aquí sale la bandera de tasas ambiguas.
    """


async def extraer(
    cliente: ClienteLLM,
    *,
    institucion: str,
    url: str,
    contenido: str,
    leida_el: date | None = None,
    max_caracteres: int = 24_000,
) -> Extraccion:
    """Tasas que publica el texto de esa página.

    Una lista vacía es un resultado legítimo y frecuente: muchas páginas del
    catálogo sólo traen publicidad. Se pide explícitamente en el prompt para
    que el modelo no sienta que debe encontrar algo.
    """
    sistema = prompts.plantilla("extract_rates_system")
    usuario = prompts.render(
        "extract_rates_user",
        institucion=institucion,
        url=url,
        fecha=(leida_el or date.today()).isoformat(),
        # Recortar por el principio sería peor: la tabla de tasas suele estar
        # arriba y el pie de página es aviso legal repetido.
        contenido=contenido[:max_caracteres],
    )

    try:
        datos, respuesta = await cliente.completar_json(
            sistema=sistema, usuario=usuario, claves_requeridas=("tasas",), max_tokens=MAX_TOKENS
        )
    except RespuestaTruncada:
        # Una página con muchas tasas no cabe en el techo, y hasta ahora eso
        # era indistinguible de una alucinación: el JSON cortado no cierra sus
        # llaves y el parser decía «no se encontró un objeto JSON». Klar caía
        # aquí en cada corrida, sin sellar el hash, así que volvía a truncarse
        # igual la siguiente. Un reintento con el doble de techo.
        log.warning("extraccion_truncada", url=url, max_tokens=MAX_TOKENS)
        datos, respuesta = await cliente.completar_json(
            sistema=sistema,
            usuario=usuario,
            claves_requeridas=("tasas",),
            max_tokens=MAX_TOKENS * 2,
        )

    try:
        extraccion = _validar(datos)
    except ValueError as exc:
        # Un reintento, con el error de validación delante. Si a la segunda
        # tampoco valida, la página queda sin extracción y eso se reporta: es
        # mejor que publicar algo que no pasó el contrato.
        log.warning("extraccion_invalida", url=url, error=str(exc)[:300])
        datos, respuesta = await cliente.completar_json(
            sistema=sistema,
            usuario=f"{usuario}\n\nTu respuesta anterior no fue válida: {exc}\n"
            f"Corrígela respetando el formato pedido.",
            claves_requeridas=("tasas",),
            max_tokens=MAX_TOKENS,
        )
        try:
            extraccion = _validar(datos)
        except ValueError as exc2:
            raise ErrorDeParseo(
                f"la extracción de {url} no validó tras el reintento: {exc2}",
                contenido_crudo=respuesta.contenido[:2000],
            ) from exc2

    log.info(
        "extraccion_ok",
        url=url,
        institucion=institucion,
        tasas=len(extraccion.tasas),
        tokens=respuesta.tokens_totales,
        costo_usd=round(respuesta.costo_usd, 6),
    )
    return extraccion


def _validar(datos: dict[str, Any]) -> Extraccion:
    """`Extraccion` o `ValueError` con el detalle legible para el reintento."""
    crudas = datos.get("tasas")
    if not isinstance(crudas, list):
        raise ValueError("'tasas' debe ser una lista")

    validas: list[TasaExtraida] = []
    errores: list[str] = []
    for i, cruda in enumerate(crudas):
        try:
            validas.append(TasaExtraida.model_validate(cruda))
        except Exception as exc:  # noqa: BLE001 — pydantic lanza su propio tipo
            errores.append(f"entrada {i}: {str(exc)[:200]}")

    # Una entrada mala no invalida las buenas: se descarta con su motivo. Que
    # el modelo confunda un plazo no debería costar las otras cuatro tasas de
    # la misma página. Todo mal sí es un reintento.
    if errores and not validas:
        raise ValueError("; ".join(errores))
    for detalle in errores:
        log.warning("tasa_extraida_descartada", detalle=detalle)
    return Extraccion(tasas=validas, ambiguas=_ambiguas(datos.get("ambiguas")))


def _ambiguas(crudas: object) -> list[str]:
    """Los reclamos descartados, saneados y acotados.

    No valida: una `ambiguas` malformada **no** puede tumbar una extracción con
    tasas buenas ni provocar un reintento pagado. Es información secundaria y
    ante la duda se calla, que es la misma regla que sigue el motor de banderas.
    """
    if not isinstance(crudas, list):
        return []
    limpias = []
    for cruda in crudas:
        if not isinstance(cruda, str):
            continue
        texto = " ".join(cruda.split())[:MAX_AMBIGUA_CARACTERES].strip()
        if texto:
            limpias.append(texto)
    return limpias[:MAX_AMBIGUAS]


__all__ = ["TASA_MAXIMA_PLAUSIBLE", "Extraccion", "TasaExtraida", "extraer"]
