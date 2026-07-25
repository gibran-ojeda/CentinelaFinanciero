"""Tasa Efectiva Neta (§4.2 del foundation).

La TEN es la métrica principal de comparación del producto: la tasa anual que
al ahorrador **realmente se le queda en el bolsillo**, después de la retención
de ISR. Es lo que permite poner en la misma columna un CETE, un pagaré bancario
y un depósito de SOFIPO sin que el usuario tenga que conocer las diferencias de
tributación entre ellos (§11, comparación justa).

Es una tasa anualizada, así que **no depende del plazo** cuando la retención va
sobre capital: un CETE a 28 días y otro a 364 días con la misma tasa nominal
tienen la misma TEN. El plazo sólo cambia los importes absolutos, que es lo que
calcula `real.desglose_cascada`.
"""

from __future__ import annotations

from decimal import Decimal

from domain.enums import TipoInstrumento
from domain.models import ParametrosFiscales
from metrics.fiscal import tasa_retencion_efectiva_anual
from metrics.rounding import PORCENTAJE, redondear


def ten(
    tasa_nominal: Decimal,
    instrumento: TipoInstrumento,
    params: ParametrosFiscales,
) -> Decimal:
    """Tasa efectiva neta anual, en porcentaje.

    Puede ser **negativa**: si la tasa nominal es menor que la de retención, el
    instrumento destruye dinero en términos nominales. No se recorta a cero a
    propósito — ocultarlo sería exactamente el tipo de letra chica que §11
    prohíbe.
    """
    resta = tasa_retencion_efectiva_anual(instrumento, tasa_nominal, params)
    return redondear(tasa_nominal - resta, PORCENTAJE)


def ten_desde_bruto_y_neto(monto: Decimal, rendimiento_neto: Decimal, plazo_dias: int) -> Decimal:
    """TEN implícita en un resultado ya calculado, anualizada.

    Útil para verificar coherencia entre la cascada y la TEN publicada, y para
    instrumentos donde el rendimiento se conoce en pesos y no como tasa.
    """
    from metrics.fiscal import factor_plazo

    if monto <= 0:
        raise ValueError("el monto debe ser positivo")
    anualizado = (rendimiento_neto / monto) / factor_plazo(plazo_dias) * 100
    return redondear(anualizado, PORCENTAJE)


__all__ = ["ten", "ten_desde_bruto_y_neto"]
