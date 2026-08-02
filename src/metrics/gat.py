"""GAT: la métrica regulada, y su equivalente calculado (§4.3 y §4.4).

La **Ganancia Anual Total** es la métrica que Banxico obliga a publicar a las
instituciones de captación. Es el equivalente al CAT del crédito, pero del lado
del ahorro. Existe ya, es comparable entre instituciones y está respaldada por
regulación: el trabajo del comparador no es inventarla sino centralizarla.

Dos variantes: **nominal** (rendimiento anual total antes de inflación) y
**real** (descontando la inflación esperada).

Para lo que no la publica —deuda gubernamental comprada en directo, IFPEs, o
productos donde la institución no la muestra— se calcula un **equivalente**
siguiendo la misma definición, y se marca como calculado. Que el usuario sepa
cuándo mira un dato regulado y cuándo una estimación nuestra es parte de la
honestidad que pide §11; por eso el resultado viaja con el origen, no suelto.

**La GAT es antes de impuestos por definición regulatoria** — la leyenda que
la disposición obliga a publicar lo dice literalmente. El equivalente tiene
que serlo también, o deja de ser equivalente: calculado sobre la TEN (que ya
descuenta el ISR), quedaba varios puntos por debajo de cualquier GAT publicada
y el orden por GAT del comparador mezclaba peras con manzanas en cuanto una
institución publicara la suya. Para el «después de impuestos» ya existe la
TEN, que es su propia columna.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from metrics.rounding import PORCENTAJE, redondear


class OrigenGat(StrEnum):
    PUBLICADA = "PUBLICADA"
    """La institución la publica. Dato regulado."""

    CALCULADA = "CALCULADA"
    """Equivalente estimado por nosotros (§4.4). Se marca como tal en la UI."""


@dataclass(frozen=True, slots=True)
class Gat:
    """GAT con su procedencia. Nunca se devuelve el número suelto."""

    nominal: Decimal
    real: Decimal
    origen: OrigenGat

    @property
    def es_calculada(self) -> bool:
        return self.origen is OrigenGat.CALCULADA


def gat_equivalente(
    tasa_nominal: Decimal,
    inflacion_anual: Decimal,
    *,
    comisiones_anuales_pct: Decimal = Decimal("0"),
) -> Gat:
    """Equivalente GAT para instrumentos que no la publican (§4.4).

    Misma definición que la GAT regulada: rendimiento anual neto de
    comisiones y **antes de impuestos**. `comisiones_anuales_pct` cubre casos
    como BONDDIA, que cobra comisión de administración descontada del
    rendimiento bruto. El ISR no entra aquí: entra en la TEN.
    """
    nominal = redondear(tasa_nominal - comisiones_anuales_pct, PORCENTAJE)
    return Gat(
        nominal=nominal,
        real=redondear(nominal - inflacion_anual, PORCENTAJE),
        origen=OrigenGat.CALCULADA,
    )


def resolver_gat(
    tasa_nominal: Decimal,
    inflacion_anual: Decimal,
    *,
    gat_publicada_nominal: Decimal | None = None,
    gat_publicada_real: Decimal | None = None,
    comisiones_anuales_pct: Decimal = Decimal("0"),
) -> Gat:
    """La GAT a mostrar: la publicada si existe, el equivalente si no.

    Es lo que consume el comparador para su columna de GAT y su criterio de
    orden. Si la institución publica la nominal pero no la real, se completa la
    real descontando inflación — sigue considerándose PUBLICADA porque el
    número de partida es el regulado.
    """
    if gat_publicada_nominal is None:
        return gat_equivalente(
            tasa_nominal,
            inflacion_anual,
            comisiones_anuales_pct=comisiones_anuales_pct,
        )

    real = (
        gat_publicada_real
        if gat_publicada_real is not None
        else redondear(gat_publicada_nominal - inflacion_anual, PORCENTAJE)
    )
    return Gat(
        nominal=redondear(gat_publicada_nominal, PORCENTAJE),
        real=redondear(real, PORCENTAJE),
        origen=OrigenGat.PUBLICADA,
    )


def gat_inconsistente(
    gat_publicada: Decimal | None,
    tasa_nominal: Decimal,
    umbral_pp: Decimal,
) -> bool:
    """¿La GAT publicada no cuadra con la tasa nominal? (§5.2).

    Una diferencia grande sugiere comisiones no evidentes o condiciones
    restrictivas: la GAT incluye todos los costos, así que debería quedar cerca
    de la nominal y por debajo de ella. Dispara una bandera 🟡 de "revisar",
    nunca una roja — es una señal para mirar el detalle, no un veredicto.

    Se comprueba el valor absoluto: una GAT muy **por encima** de la nominal es
    igual de sospechosa que una muy por debajo, porque no debería poder serlo.
    """
    if gat_publicada is None:
        return False
    return abs(gat_publicada - tasa_nominal) > umbral_pp


__all__ = ["Gat", "OrigenGat", "gat_equivalente", "gat_inconsistente", "resolver_gat"]
