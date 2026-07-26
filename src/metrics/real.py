"""Ganancia real después de inflación (§4.5 y §6 del foundation).

El número más honesto que se le puede mostrar a un ahorrador: cuánto creció de
verdad su poder adquisitivo, no cuánto subió el saldo.

La calculadora presenta cinco conceptos **en cascada, no en tabla** (§6): de la
ganancia bruta se va una parte a impuestos, otra se la come la inflación, y lo
que queda es la ganancia real. La cascada tiene que cuadrar exactamente, sin
"aproximadamente": por eso cada concepto se deriva por resta de los anteriores
ya redondeados, en vez de calcularse por separado y redondearse al final. Así
`bruto = ISR + inflación + real` es una identidad, no una tolerancia.

Ese detalle importa porque el usuario va a sumar los números que ve.
"""

from __future__ import annotations

from decimal import Decimal

from domain.enums import TipoInstrumento
from domain.models import DesgloseCascada, ParametrosFiscales
from metrics.fiscal import factor_plazo, nota_fiscal, rendimiento_bruto, retencion_isr
from metrics.rounding import CENTAVO, redondear
from metrics.ten import ten


def efecto_inflacion(monto: Decimal, inflacion_anual: Decimal, plazo_dias: int) -> Decimal:
    """Poder adquisitivo que pierde el capital durante el plazo.

    No es un cargo: nadie lo cobra. Es cuánto tendría que rendir el dinero sólo
    para conservar su valor, y por eso se muestra junto a los impuestos — las
    dos cosas que separan el rendimiento de la ganancia.
    """
    return redondear(monto * (inflacion_anual / 100) * factor_plazo(plazo_dias), CENTAVO)


def desglose_cascada(
    monto: Decimal,
    tasa_nominal: Decimal,
    instrumento: TipoInstrumento,
    plazo_dias: int,
    inflacion_anual: Decimal,
    params: ParametrosFiscales,
) -> DesgloseCascada:
    """Los cinco conceptos de §6, con la nota fiscal que los acompaña.

    Cada concepto sale por resta del anterior para que la cascada cuadre al
    centavo. La ganancia real puede ser negativa, y lo será a menudo: es
    precisamente el hallazgo que justifica el producto.
    """
    if monto <= 0:
        raise ValueError("el monto debe ser positivo")

    bruto = rendimiento_bruto(monto, tasa_nominal, plazo_dias)
    isr = retencion_isr(instrumento, monto, tasa_nominal, plazo_dias, params)
    neto = redondear(bruto - isr, CENTAVO)
    inflacion = efecto_inflacion(monto, inflacion_anual, plazo_dias)
    real = redondear(neto - inflacion, CENTAVO)

    return DesgloseCascada(
        monto_invertido=redondear(monto, CENTAVO),
        rendimiento_bruto=bruto,
        isr_retenido=isr,
        rendimiento_neto=neto,
        efecto_inflacion=inflacion,
        ganancia_real=real,
        plazo_dias=plazo_dias,
        tasa_nominal=tasa_nominal,
        ten=ten(tasa_nominal, instrumento, params),
        inflacion_anual=inflacion_anual,
        nota_fiscal=nota_fiscal(instrumento, params),
    )


def ganancia_real_anual(
    monto: Decimal,
    tasa_nominal: Decimal,
    instrumento: TipoInstrumento,
    inflacion_anual: Decimal,
    params: ParametrosFiscales,
) -> Decimal:
    """Fórmula literal de §4.5: monto × TEN − monto × inflación."""
    return redondear(
        monto * (ten(tasa_nominal, instrumento, params) - inflacion_anual) / 100, CENTAVO
    )


__all__ = ["desglose_cascada", "efecto_inflacion", "ganancia_real_anual"]
