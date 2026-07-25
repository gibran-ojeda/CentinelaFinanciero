"""Tratamiento fiscal por tipo de instrumento (§4.2 del foundation).

Todo el módulo es de funciones puras sobre `Decimal`. No lee configuración ni
base de datos: los parámetros entran como argumento. Es lo que permite testear
la matemática sin infraestructura y lo que garantiza que dos llamadas con las
mismas entradas den lo mismo, hoy y dentro de un año.

**Cómo funciona la retención en México.** El artículo 54 de la LISR establece
una retención provisional que no se calcula sobre el interés cobrado sino
sobre el *capital que da lugar a ese interés*, aplicando la tasa anual que fija
cada año el artículo 24 de la Ley de Ingresos. Es acreditable en la declaración
anual contra el ISR real, que sí se calcula sobre el interés real (nominal
menos inflación).

Consecuencia contraintuitiva y central para este producto: **la retención no
depende de cuánto rindió el instrumento**. Con tasas bajas puede superar al
rendimiento y dejar la ganancia neta en negativo. Por eso la TEN es la métrica
de comparación y no la tasa nominal.

**Dos divergencias respecto al foundation v1.3**, ambas verificadas contra la
fuente y ambas por el mismo motivo: el documento describe el estado de 2025.

1. *La tasa es 0.90%, no ~0.50%.* Artículo 24 de la LIF 2026 (DOF 7-nov-2025),
   vigente desde el 1 de enero de 2026. Aquí no se hardcodea ninguna de las
   dos: entra por `ParametrosFiscales`, que es exactamente lo que §4.2 pide
   ("la plataforma debe actualizarla cuando haya modificaciones fiscales").
2. *Los fondos de deuda retienen sobre capital, no sobre ganancia.* El
   artículo 87 de la LISR releva al emisor de retener y remite al régimen del
   artículo 54, que es sobre capital. La regla de la RMF lo confirma con su
   fórmula de retención diaria (`0.00139% × promedio diario × días`, que es
   0.50%/360). El foundation lo describe como "retención sobre ganancia".
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from domain.enums import TipoInstrumento
from domain.models import ParametrosFiscales
from metrics.rounding import CENTAVO, PORCENTAJE, redondear

#: Base de días para prorratear tasas anuales.
#:
#: 360 y no 365: es la convención del mercado de dinero mexicano y la que usa
#: la propia regla de la RMF para la retención (0.00139% diario × 360 = 0.50%
#: anual). Se aplica igual al rendimiento bruto, a la retención y al efecto de
#: la inflación, porque mezclar bases dentro de una misma cascada produciría
#: descuadres de centavos sin significado económico.
BASE_ANUAL_DIAS = Decimal("360")


class BaseRetencion(StrEnum):
    """Sobre qué se calcula la retención."""

    CAPITAL = "CAPITAL"
    """Sobre el monto invertido, con independencia del rendimiento (art. 54)."""

    GANANCIA = "GANANCIA"
    """Sobre el interés generado. Ningún instrumento del catálogo la usa hoy;
    se conserva porque §4.2 la contempla y las extensiones de la fase 10
    pueden incorporar estructuras que sí tributen así."""

    NINGUNA = "NINGUNA"
    """Sin retención en la fuente."""


@dataclass(frozen=True, slots=True)
class TratamientoFiscal:
    base: BaseRetencion
    #: El ajuste por inflación no causa ISR hasta el vencimiento en ciertos
    #: esquemas (§4.2, caso UDIBONOS). Es una diferencia de *momento*, no de
    #: tasa: la retención provisional sobre capital se aplica igual.
    ajuste_inflacionario_diferido: bool = False
    detalle: str = ""


#: Tratamiento por instrumento. La tabla, y no una cadena de `if`, para que
#: añadir un instrumento en la fase 10 sea añadir una fila.
TRATAMIENTO_POR_INSTRUMENTO: dict[TipoInstrumento, TratamientoFiscal] = {
    TipoInstrumento.CETES: TratamientoFiscal(BaseRetencion.CAPITAL),
    TipoInstrumento.BONDDIA: TratamientoFiscal(BaseRetencion.CAPITAL),
    TipoInstrumento.BONOS_M: TratamientoFiscal(BaseRetencion.CAPITAL),
    TipoInstrumento.BONDES_D: TratamientoFiscal(BaseRetencion.CAPITAL),
    TipoInstrumento.UDIBONOS: TratamientoFiscal(
        BaseRetencion.CAPITAL,
        ajuste_inflacionario_diferido=True,
        detalle=(
            "El ajuste inflacionario del principal (valor UDI) no causa ISR hasta el "
            "vencimiento en ciertos esquemas; la retención provisional sobre el capital "
            "se aplica igual."
        ),
    ),
    TipoInstrumento.PRLV: TratamientoFiscal(BaseRetencion.CAPITAL),
    TipoInstrumento.DEPOSITO_SOFIPO: TratamientoFiscal(BaseRetencion.CAPITAL),
    TipoInstrumento.DEPOSITO_BANCARIO: TratamientoFiscal(BaseRetencion.CAPITAL),
    TipoInstrumento.FONDO_DEUDA: TratamientoFiscal(
        BaseRetencion.CAPITAL,
        detalle=(
            "El fondo no es contribuyente (art. 87 LISR): la retención la soporta el "
            "inversionista bajo el régimen del art. 54, sobre el capital. La comisión "
            "de administración del fondo se descuenta aparte, antes del ISR."
        ),
    ),
    TipoInstrumento.MONEDERO_ELECTRONICO: TratamientoFiscal(
        BaseRetencion.CAPITAL,
        detalle=(
            "El rendimiento suele provenir de un fondo de deuda subyacente y sigue su "
            "mismo régimen. Sin cobertura IPAB ni PROSOFIPO."
        ),
    ),
}


def tratamiento(instrumento: TipoInstrumento) -> TratamientoFiscal:
    """Tratamiento aplicable. Falla si el instrumento no está en la tabla.

    Deliberadamente sin `default`: un instrumento nuevo sin tratamiento fiscal
    declarado debe romper en tests, no calcular en silencio con una suposición.
    """
    try:
        return TRATAMIENTO_POR_INSTRUMENTO[instrumento]
    except KeyError as exc:
        raise KeyError(
            f"{instrumento} no tiene tratamiento fiscal declarado en "
            f"metrics.fiscal.TRATAMIENTO_POR_INSTRUMENTO"
        ) from exc


def factor_plazo(plazo_dias: int) -> Decimal:
    """Fracción de año que representa el plazo, en base 360."""
    if plazo_dias <= 0:
        raise ValueError(f"plazo_dias debe ser positivo, se recibió {plazo_dias}")
    return Decimal(plazo_dias) / BASE_ANUAL_DIAS


def rendimiento_bruto(monto: Decimal, tasa_nominal: Decimal, plazo_dias: int) -> Decimal:
    """Interés antes de impuestos que genera `monto` en `plazo_dias`."""
    return redondear(monto * (tasa_nominal / 100) * factor_plazo(plazo_dias), CENTAVO)


def retencion_isr(
    instrumento: TipoInstrumento,
    monto: Decimal,
    tasa_nominal: Decimal,
    plazo_dias: int,
    params: ParametrosFiscales,
) -> Decimal:
    """ISR retenido en la fuente durante el plazo, en pesos.

    `tasa_nominal` sólo interviene si la base es GANANCIA; con base CAPITAL la
    retención es independiente del rendimiento, que es justamente el punto.
    """
    trato = tratamiento(instrumento)
    factor = factor_plazo(plazo_dias)

    match trato.base:
        case BaseRetencion.CAPITAL:
            bruto_retencion = monto * (params.tasa_retencion_capital / 100) * factor
        case BaseRetencion.GANANCIA:
            interes = monto * (tasa_nominal / 100) * factor
            bruto_retencion = interes * (params.tasa_retencion_ganancia / 100)
        case _:
            bruto_retencion = Decimal("0")

    return redondear(bruto_retencion, CENTAVO)


def tasa_retencion_efectiva_anual(
    instrumento: TipoInstrumento, tasa_nominal: Decimal, params: ParametrosFiscales
) -> Decimal:
    """Cuánto resta la retención a la tasa nominal, en puntos porcentuales.

    Con base CAPITAL es la tasa de retención tal cual; con base GANANCIA es
    proporcional al rendimiento. Lo consume `ten.py`.
    """
    trato = tratamiento(instrumento)
    match trato.base:
        case BaseRetencion.CAPITAL:
            resta = params.tasa_retencion_capital
        case BaseRetencion.GANANCIA:
            resta = tasa_nominal * (params.tasa_retencion_ganancia / 100)
        case _:
            resta = Decimal("0")
    return redondear(resta, PORCENTAJE)


def nota_fiscal(instrumento: TipoInstrumento, params: ParametrosFiscales) -> str:
    """Texto que §6 obliga a mostrar junto a cualquier cálculo.

    Dice qué retención se aplicó y desde cuándo, en lenguaje llano y sin
    tecnicismos, como pide el principio de honestidad fiscal de §11.
    """
    trato = tratamiento(instrumento)

    match trato.base:
        case BaseRetencion.CAPITAL:
            cabeza = (
                f"Se aplicó una retención de ISR del {params.tasa_retencion_capital}% anual "
                f"sobre el monto invertido (no sobre la ganancia), vigente desde el "
                f"{params.vigente_desde.isoformat()}."
            )
        case BaseRetencion.GANANCIA:
            cabeza = (
                f"Se aplicó una retención de ISR del {params.tasa_retencion_ganancia}% "
                f"sobre el rendimiento generado, vigente desde el "
                f"{params.vigente_desde.isoformat()}."
            )
        case _:
            cabeza = "Este instrumento no causa retención de ISR en la fuente."

    partes = [cabeza]
    if trato.detalle:
        partes.append(trato.detalle)
    partes.append(
        "La retención es provisional y acreditable en tu declaración anual, donde el "
        "ISR definitivo se calcula sobre el interés real (nominal menos inflación)."
    )
    return " ".join(partes)


__all__ = [
    "BASE_ANUAL_DIAS",
    "TRATAMIENTO_POR_INSTRUMENTO",
    "BaseRetencion",
    "TratamientoFiscal",
    "factor_plazo",
    "nota_fiscal",
    "rendimiento_bruto",
    "retencion_isr",
    "tasa_retencion_efectiva_anual",
    "tratamiento",
]
