"""Tests de la reconstrucción de escaleras desde extracciones del nivel 2.

Puro: sin base de datos ni LLM. La materia prima son `TasaExtraida` como las
que produce el extractor bajo la regla 4 («un tramo por monto es una entrada
por tramo»).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from domain.enums import TipoProducto
from metrics.tramos import Tramo
from rates_agent.escalera import (
    colapsar_por_condicion,
    reconstruir_escalera,
    render_escalera,
)
from rates_agent.extractor import TasaExtraida


def _entrada(
    tasa: str,
    monto_minimo: str | None,
    *,
    monto_maximo: str | None = None,
    confianza: str = "alta",
    condiciones: str | None = None,
) -> TasaExtraida:
    return TasaExtraida(
        producto="Cuenta de ahorro",
        tipo=TipoProducto.VISTA,
        tasa_nominal=Decimal(tasa),
        monto_minimo=Decimal(monto_minimo) if monto_minimo is not None else None,
        monto_maximo=Decimal(monto_maximo) if monto_maximo is not None else None,
        confianza=confianza,  # type: ignore[arg-type]
        condiciones=condiciones,
    )


def test_two_entries_with_distinct_floors_become_a_ladder() -> None:
    escalera = reconstruir_escalera([_entrada("6.30", "30000"), _entrada("13.00", "0")])

    assert escalera is not None
    assert escalera.tramos == (
        Tramo(desde=Decimal("0"), hasta=Decimal("30000"), tasa_nominal=Decimal("13.00")),
        Tramo(desde=Decimal("30000"), hasta=None, tasa_nominal=Decimal("6.30")),
    )
    # La cabeza es la entrada del tramo 1: aporta la tasa titular.
    assert escalera.cabeza.tasa_nominal == Decimal("13.00")


def test_a_missing_floor_counts_as_zero() -> None:
    """El extractor puede omitir el mínimo del tramo base: es el primer peso."""
    escalera = reconstruir_escalera([_entrada("13.00", None), _entrada("6.30", "30000")])

    assert escalera is not None
    assert escalera.tramos[0].desde == Decimal("0")


def test_a_single_entry_without_a_ceiling_is_not_a_ladder() -> None:
    """Sin tope no hay escalón: es una tasa plana, y así viaja."""
    assert reconstruir_escalera([_entrada("13.00", "0")]) is None
    assert reconstruir_escalera([]) is None


def test_a_single_capped_entry_declares_what_the_excess_earns() -> None:
    """«15 % en tus primeros $25,000» y silencio por encima.

    Es el caso de Revolut medido el 2026-08-06, y el que hacía desaparecer el
    tope: sin `monto_maximo` la entrada quedaba plana y la tabla prometía 15 %
    sobre cualquier saldo. El excedente se declara al 0 % —lo único que la
    página afirma sobre ese dinero— en vez de dejarlo implícito.
    """
    escalera = reconstruir_escalera([_entrada("15.00", "0", monto_maximo="25000")])

    assert escalera is not None
    assert escalera.tramos == (
        Tramo(desde=Decimal("0"), hasta=Decimal("25000"), tasa_nominal=Decimal("15.00")),
        Tramo(desde=Decimal("25000"), hasta=None, tasa_nominal=Decimal("0")),
    )
    assert escalera.cabeza.tasa_nominal == Decimal("15.00")


def test_the_last_tier_keeps_the_ceiling_the_page_published() -> None:
    """La escalera completa de Revolut: dos tramos, el segundo acotado.

    Antes de `monto_maximo` el último techo se forzaba a infinito, así que ni
    siquiera un Openbank leído por fetch podía reproducir su «hasta $1 M» — la
    única escalera con techo del sistema estaba escrita a mano en el CSV.
    """
    escalera = reconstruir_escalera(
        [
            _entrada("15.00", "0", monto_maximo="25000"),
            _entrada("7.00", "25000", monto_maximo="1000000"),
        ]
    )

    assert escalera is not None
    assert escalera.tramos == (
        Tramo(desde=Decimal("0"), hasta=Decimal("25000"), tasa_nominal=Decimal("15.00")),
        Tramo(desde=Decimal("25000"), hasta=Decimal("1000000"), tasa_nominal=Decimal("7.00")),
    )


def test_the_next_floor_wins_over_a_declared_ceiling() -> None:
    """Contigüidad exacta: el techo intermedio sale del conjunto, no de la fila.

    Si la página declara «hasta $30,000» en el primer tramo y arranca el
    siguiente en $25,000, respetar los dos dejaría un solape que
    `validar_escalera` rechazaría y la escalera entera se perdería.
    """
    escalera = reconstruir_escalera(
        [_entrada("15.00", "0", monto_maximo="30000"), _entrada("7.00", "25000")]
    )

    assert escalera is not None
    assert escalera.tramos[0].hasta == Decimal("25000")
    assert escalera.tramos[1].hasta is None


def test_two_open_entries_still_end_at_infinity() -> None:
    """Sin techo declarado, el último tramo sigue cubriendo cualquier saldo."""
    escalera = reconstruir_escalera([_entrada("13.00", "0"), _entrada("6.30", "30000")])

    assert escalera is not None
    assert escalera.tramos[-1].hasta is None


def test_a_ceiling_below_its_own_floor_is_rejected() -> None:
    with pytest.raises(ValidationError, match="tramo sin recorrido"):
        _entrada("7.00", "25000", monto_maximo="1000")


def test_duplicate_floors_are_irreconstructible() -> None:
    """Dos tramos con el mismo piso: no se sabe dónde corta cada uno."""
    assert reconstruir_escalera([_entrada("13.00", "0"), _entrada("6.30", "0")]) is None
    assert reconstruir_escalera([_entrada("13.00", None), _entrada("6.30", "0")]) is None


def test_a_ladder_that_does_not_start_at_zero_is_irreconstructible() -> None:
    assert reconstruir_escalera([_entrada("13.00", "1000"), _entrada("6.30", "30000")]) is None


def test_the_head_carries_the_worst_confidence_of_the_group() -> None:
    """La escalera vale lo que su tramo menos fiable: es lo que el reviewer
    mira para mandar a revisión."""
    escalera = reconstruir_escalera(
        [_entrada("13.00", "0", confianza="alta"), _entrada("6.30", "30000", confianza="baja")]
    )

    assert escalera is not None
    assert escalera.cabeza.confianza == "baja"


def test_shared_conditions_stay_as_one() -> None:
    escalera = reconstruir_escalera(
        [
            _entrada("13.00", "0", condiciones="Tasa antes de impuestos."),
            _entrada("6.30", "30000", condiciones="Tasa antes de impuestos."),
        ]
    )

    assert escalera is not None
    assert escalera.condiciones == "Tasa antes de impuestos."


def test_diverging_conditions_concatenate_by_tier() -> None:
    escalera = reconstruir_escalera(
        [
            _entrada("13.00", "0", condiciones="Requiere nómina."),
            _entrada("6.30", "30000", condiciones="Sin requisitos."),
        ]
    )

    assert escalera is not None
    assert escalera.condiciones == (
        "$0–$30,000: Requiere nómina. · $30,000 en adelante: Sin requisitos."
    )


def test_membership_variants_collapse_into_the_lowest() -> None:
    """El caso Hey, medido el 2026-08-07.

    Publica 4.00 % como Cliente Hey y 7.50 % siendo Fan Hey o Hey Pro, y el
    modelo devolvió las dos como entradas del mismo producto y plazo. No son
    tramos —comparten piso— así que el grupo entero se caía como hueco.
    """
    colapsada = colapsar_por_condicion(
        [
            _entrada("7.50", None, condiciones="Tasa exclusiva Fan Hey / Hey Pro"),
            _entrada("4.00", None, condiciones="Cliente Hey"),
        ]
    )

    assert colapsada is not None
    assert colapsada.tasa_nominal == Decimal("4.00")
    assert colapsada.condiciones == "Cliente Hey · 7.50%: Tasa exclusiva Fan Hey / Hey Pro"
    # La eligió el sistema, no la leyó nadie: la confianza no puede ser alta.
    assert colapsada.confianza == "media"


def test_a_variant_without_conditions_still_says_there_is_one() -> None:
    colapsada = colapsar_por_condicion([_entrada("7.50", "0"), _entrada("4.00", "0")])

    assert colapsada is not None
    assert colapsada.condiciones == "7.50% bajo una condición que la página no detalla"


def test_distinct_floors_are_never_collapsed() -> None:
    """Pisos distintos son tramos por monto, aunque la escalera no cuadre.

    «13 % desde $1,000» y «6.30 % desde $30,000» tiene un tramo base que la
    página no declaró, y la regla 1 prohíbe inventarlo: eso sigue siendo hueco,
    no un colapso a la más baja.
    """
    assert colapsar_por_condicion([_entrada("13.00", "1000"), _entrada("6.30", "30000")]) is None
    assert colapsar_por_condicion([_entrada("13.00", "0")]) is None


def test_the_worst_confidence_survives_the_collapse() -> None:
    colapsada = colapsar_por_condicion(
        [_entrada("7.50", None, confianza="alta"), _entrada("4.00", None, confianza="baja")]
    )

    assert colapsada is not None
    assert colapsada.confianza == "baja"


def test_render_reads_in_one_terminal_line() -> None:
    escalera = reconstruir_escalera([_entrada("13.00", "0"), _entrada("6.30", "30000")])

    assert escalera is not None
    assert render_escalera(escalera.tramos) == "$0–$30,000: 13.00% · $30,000 en adelante: 6.30%"
    assert render_escalera(()) == "plana"
