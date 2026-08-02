"""Tests de la reconstrucción de escaleras desde extracciones del nivel 2.

Puro: sin base de datos ni LLM. La materia prima son `TasaExtraida` como las
que produce el extractor bajo la regla 4 («un tramo por monto es una entrada
por tramo»).
"""

from __future__ import annotations

from decimal import Decimal

from domain.enums import TipoProducto
from metrics.tramos import Tramo
from rates_agent.escalera import reconstruir_escalera, render_escalera
from rates_agent.extractor import TasaExtraida


def _entrada(
    tasa: str,
    monto_minimo: str | None,
    *,
    confianza: str = "alta",
    condiciones: str | None = None,
) -> TasaExtraida:
    return TasaExtraida(
        producto="Cuenta de ahorro",
        tipo=TipoProducto.VISTA,
        tasa_nominal=Decimal(tasa),
        monto_minimo=Decimal(monto_minimo) if monto_minimo is not None else None,
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


def test_a_single_entry_is_not_a_ladder() -> None:
    assert reconstruir_escalera([_entrada("13.00", "0")]) is None


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


def test_render_reads_in_one_terminal_line() -> None:
    escalera = reconstruir_escalera([_entrada("13.00", "0"), _entrada("6.30", "30000")])

    assert escalera is not None
    assert render_escalera(escalera.tramos) == "$0–$30,000: 13.00% · $30,000 en adelante: 6.30%"
    assert render_escalera(()) == "plana"
