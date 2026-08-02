"""Tasas escalonadas por tramo de saldo.

Openbank paga 13% por los primeros $30,000 y 6.3% de ahí a $1,000,000: la tasa
que ese producto rinde de verdad depende del monto invertido. Este módulo es
la única aritmética de escaleras del sistema — validación, tasa ponderada y su
TEN — y es puro: `Decimal`, sin base de datos, como todo `metrics/`.

Dos decisiones de diseño viven aquí y no en el esquema:

- **La validación estructural es de escritura, no de DDL.** El no-solape y la
  contigüidad no caben en un CHECK portable (EXCLUDE USING gist es
  solo-Postgres y los tests corren también sobre SQLite), así que
  `validar_escalera` es el constructor por el que pasan todos los escritores
  (reviewer del fetch, CSV manual, seeds).
- **El excedente por encima del último techo publicado rinde 0.** Si la
  institución dice «6.3% de $30,000 a $1,000,000» y calla por encima,
  asignarle el 6.3% al excedente sería exactamente el «hasta X%» que la regla
  1 del prompt del extractor prohíbe inventar; 0 es lo único que la página
  afirma (nada) y es conservador — subestima, nunca promete de más.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from domain.enums import TipoInstrumento
from domain.models import ParametrosFiscales
from metrics.rounding import PORCENTAJE, redondear
from metrics.ten import ten

_CERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class Tramo:
    """Un escalón de la escalera: `[desde, hasta)` con su tasa nominal anual.

    `hasta` en `None` significa «sin techo publicado» — el tramo se extiende a
    cualquier saldo.
    """

    desde: Decimal
    hasta: Decimal | None
    tasa_nominal: Decimal


def validar_escalera(tramos: Sequence[Tramo]) -> tuple[Tramo, ...]:
    """Ordena y valida una escalera completa; es EL constructor.

    Reglas: primer piso en 0 (una escalera que no cubre desde el primer peso
    tiene un tramo base que nadie declaró), contigüidad exacta (el techo de
    cada tramo es el piso del siguiente), techo abierto solo en el último, y
    tasas no negativas.

    Normalizaciones: la escalera vacía es válida (tasa plana) y devuelve
    `()`; un único tramo `[0, ∞)` también se normaliza a `()` — es una tasa
    plana que llegó disfrazada. Un único tramo CON techo se rechaza: si por
    encima del techo no rinde, el escritor debe declararlo con un tramo a 0,
    no dejarlo implícito.
    """
    if not tramos:
        return ()

    ordenados = tuple(sorted(tramos, key=lambda t: t.desde))

    pisos = [t.desde for t in ordenados]
    if len(set(pisos)) != len(pisos):
        raise ValueError("escalera inválida: dos tramos comparten el mismo piso")
    if ordenados[0].desde != _CERO:
        raise ValueError(
            f"escalera inválida: el primer tramo empieza en {ordenados[0].desde}, no en 0"
        )

    for tramo in ordenados:
        if tramo.tasa_nominal < _CERO:
            raise ValueError(f"escalera inválida: tasa negativa en el tramo desde {tramo.desde}")

    for anterior, siguiente in zip(ordenados, ordenados[1:], strict=False):
        if anterior.hasta is None:
            raise ValueError(
                "escalera inválida: solo el último tramo puede quedar sin techo, "
                f"y el tramo desde {anterior.desde} no es el último"
            )
        if anterior.hasta != siguiente.desde:
            raise ValueError(
                f"escalera inválida: el tramo desde {anterior.desde} termina en "
                f"{anterior.hasta} y el siguiente empieza en {siguiente.desde}"
            )

    ultimo = ordenados[-1]
    if ultimo.hasta is not None and ultimo.hasta <= ultimo.desde:
        raise ValueError(
            f"escalera inválida: el techo {ultimo.hasta} no supera al piso {ultimo.desde}"
        )

    if len(ordenados) == 1:
        if ordenados[0].hasta is not None:
            raise ValueError(
                "escalera inválida: un único tramo con techo deja el excedente sin "
                "declarar; si por encima no rinde, añade un tramo con tasa 0"
            )
        return ()

    return ordenados


def escalera_de(tasa_nominal: Decimal, tramos: Sequence[Tramo]) -> tuple[Tramo, ...]:
    """La escalera efectiva de una observación, plana o no.

    Sin tramos, una tasa plana ES la escalera trivial `[0, ∞)`. Con tramos —
    que ya pasaron por `validar_escalera` al escribirse — se exige la
    invariante del sistema: `Tasa.tasa_nominal` es siempre la tasa del primer
    tramo, y una discrepancia aquí es un dato corrupto que no debe seguir
    viajando en silencio.
    """
    if not tramos:
        return (Tramo(desde=_CERO, hasta=None, tasa_nominal=tasa_nominal),)
    primero = min(tramos, key=lambda t: t.desde)
    if primero.tasa_nominal != tasa_nominal:
        raise ValueError(
            f"escalera incoherente: la tasa titular es {tasa_nominal} pero el primer "
            f"tramo paga {primero.tasa_nominal}"
        )
    return tuple(sorted(tramos, key=lambda t: t.desde))


def tasa_ponderada(monto: Decimal, tramos: Sequence[Tramo]) -> Decimal:
    """Tasa nominal anual efectiva de un `monto` repartido sobre la escalera.

    `Σ tasa_i × capacidad_i / monto`, donde la capacidad de cada tramo es el
    dinero del monto que cae dentro de `[desde, hasta)`. Lo que quede por
    encima del último techo publicado no suma nada — rinde 0 (ver docstring
    del módulo). Redondeada a cuatro decimales, la misma cuantía que las
    tasas persistidas: ponderar primero y calcular después mantiene coherente
    lo que el usuario ve con lo que los importes suman.
    """
    if monto <= _CERO:
        raise ValueError("el monto debe ser positivo")
    if not tramos:
        raise ValueError("escalera vacía: usa escalera_de para obtener la trivial")

    acumulado = _CERO
    for tramo in tramos:
        techo = monto if tramo.hasta is None else min(monto, tramo.hasta)
        capacidad = techo - tramo.desde
        if capacidad > _CERO:
            acumulado += tramo.tasa_nominal * capacidad

    return redondear(acumulado / monto, PORCENTAJE)


def ten_efectiva(
    monto: Decimal,
    tramos: Sequence[Tramo],
    instrumento: TipoInstrumento,
    params: ParametrosFiscales,
) -> Decimal:
    """TEN de la tasa ponderada al monto dado.

    Delega en `metrics.ten` para que la retención siga viviendo solo en
    `fiscal.py`. Como la TEN es afín en la nominal —base CAPITAL resta una
    constante; base GANANCIA multiplica por una— la TEN de la ponderada
    coincide con la ponderada de las TEN por tramo: vale para las dos bases,
    hoy y cuando la fase 10 traiga instrumentos con base GANANCIA.
    """
    return ten(tasa_ponderada(monto, tramos), instrumento, params)


__all__ = ["Tramo", "escalera_de", "tasa_ponderada", "ten_efectiva", "validar_escalera"]
