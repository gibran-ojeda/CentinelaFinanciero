"""Redondeo consistente para todo el motor de métricas.

Un comparador financiero no puede permitirse que dos pantallas muestren
$2,499.99 y $2,500.00 para el mismo cálculo. Todo el módulo `metrics` redondea
por aquí, con `ROUND_HALF_UP` —el redondeo que la gente espera, no el bancario
de Python por defecto— y con dos cuantías fijas: centavos para dinero, cuatro
decimales para tasas.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

#: Dinero: dos decimales. Es la unidad mínima en la que se puede cobrar.
CENTAVO = Decimal("0.01")

#: Tasas y porcentajes: cuatro decimales. Coincide con Numeric(8, 4) del ORM,
#: así que un valor calculado y uno recuperado de la base comparan igual.
PORCENTAJE = Decimal("0.0001")


def redondear(valor: Decimal, cuantia: Decimal = CENTAVO) -> Decimal:
    """Redondea con `ROUND_HALF_UP`.

    Python usa `ROUND_HALF_EVEN` por defecto, que redondea 0.125 a 0.12. Nadie
    fuera de contabilidad espera eso, y en una calculadora que el usuario va a
    contrastar con su banco produce discrepancias inexplicables.
    """
    return valor.quantize(cuantia, rounding=ROUND_HALF_UP)


__all__ = ["CENTAVO", "PORCENTAJE", "redondear"]
