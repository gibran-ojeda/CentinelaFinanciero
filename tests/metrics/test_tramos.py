"""Tests de la aritmética de escaleras por tramo de saldo.

Los números canónicos son los de Openbank —13% los primeros $30,000, 6.3% de
ahí a $1,000,000— porque es el caso real que motivó el modelo y el que la
documentación usa de ejemplo.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from domain.enums import TipoInstrumento
from domain.models import ParametrosFiscales
from metrics.tramos import Tramo, escalera_de, tasa_ponderada, ten_efectiva, validar_escalera

OPENBANK = (
    Tramo(desde=Decimal("0"), hasta=Decimal("30000"), tasa_nominal=Decimal("13.00")),
    Tramo(desde=Decimal("30000"), hasta=Decimal("1000000"), tasa_nominal=Decimal("6.30")),
)


# ─── tasa_ponderada ───────────────────────────────────────────


def test_a_blended_rate_weighs_each_tier_by_its_capacity() -> None:
    # (30,000 × 13 + 20,000 × 6.3) / 50,000
    assert tasa_ponderada(Decimal("50000"), OPENBANK) == Decimal("10.3200")


def test_an_amount_inside_the_first_tier_earns_the_headline_rate() -> None:
    assert tasa_ponderada(Decimal("20000"), OPENBANK) == Decimal("13.0000")


def test_the_excess_above_the_last_published_ceiling_earns_zero() -> None:
    """Lo que la página no afirma no rinde: sería inventar un «hasta X%»."""
    # (30,000 × 13 + 970,000 × 6.3 + 500,000 × 0) / 1,500,000
    assert tasa_ponderada(Decimal("1500000"), OPENBANK) == Decimal("4.3340")


def test_a_flat_ladder_is_the_identity() -> None:
    escalera = escalera_de(Decimal("7.25"), ())
    assert tasa_ponderada(Decimal("123456.78"), escalera) == Decimal("7.2500")


def test_a_non_positive_amount_is_rejected() -> None:
    with pytest.raises(ValueError, match="positivo"):
        tasa_ponderada(Decimal("0"), OPENBANK)


def test_an_empty_ladder_is_rejected() -> None:
    with pytest.raises(ValueError, match="escalera vacía"):
        tasa_ponderada(Decimal("100"), ())


# ─── ten_efectiva ─────────────────────────────────────────────


def test_the_effective_ten_subtracts_the_capital_retention(
    fiscal_2026: ParametrosFiscales,
) -> None:
    resultado = ten_efectiva(
        Decimal("50000"), OPENBANK, TipoInstrumento.DEPOSITO_BANCARIO, fiscal_2026
    )
    assert resultado == Decimal("9.4200")  # 10.32 − 0.90


# ─── validar_escalera ─────────────────────────────────────────


def test_a_valid_ladder_comes_back_sorted() -> None:
    desordenada = (OPENBANK[1], OPENBANK[0])
    assert validar_escalera(desordenada) == OPENBANK


def test_an_empty_ladder_is_a_flat_rate() -> None:
    assert validar_escalera(()) == ()


def test_a_single_unbounded_tier_normalizes_to_flat() -> None:
    """Una escalera de un tramo [0, ∞) es una tasa plana disfrazada."""
    assert validar_escalera((Tramo(Decimal("0"), None, Decimal("13")),)) == ()


def test_a_single_bounded_tier_is_rejected() -> None:
    """El excedente no puede quedar implícito: se declara con tasa 0."""
    with pytest.raises(ValueError, match="excedente"):
        validar_escalera((Tramo(Decimal("0"), Decimal("30000"), Decimal("13")),))


def test_a_ladder_that_does_not_start_at_zero_is_rejected() -> None:
    tramos = (
        Tramo(Decimal("1000"), Decimal("30000"), Decimal("13")),
        Tramo(Decimal("30000"), None, Decimal("6.3")),
    )
    with pytest.raises(ValueError, match="no en 0"):
        validar_escalera(tramos)


def test_a_gap_between_tiers_is_rejected() -> None:
    tramos = (
        Tramo(Decimal("0"), Decimal("30000"), Decimal("13")),
        Tramo(Decimal("50000"), None, Decimal("6.3")),
    )
    with pytest.raises(ValueError, match="termina en"):
        validar_escalera(tramos)


def test_duplicate_floors_are_rejected() -> None:
    tramos = (
        Tramo(Decimal("0"), Decimal("30000"), Decimal("13")),
        Tramo(Decimal("0"), None, Decimal("6.3")),
    )
    with pytest.raises(ValueError, match="mismo piso"):
        validar_escalera(tramos)


def test_only_the_last_tier_may_be_unbounded() -> None:
    tramos = (
        Tramo(Decimal("0"), None, Decimal("13")),
        Tramo(Decimal("30000"), None, Decimal("6.3")),
    )
    with pytest.raises(ValueError, match="último"):
        validar_escalera(tramos)


def test_a_negative_tier_rate_is_rejected() -> None:
    tramos = (
        Tramo(Decimal("0"), Decimal("30000"), Decimal("-1")),
        Tramo(Decimal("30000"), None, Decimal("6.3")),
    )
    with pytest.raises(ValueError, match="negativa"):
        validar_escalera(tramos)


# ─── escalera_de ──────────────────────────────────────────────


def test_a_flat_rate_becomes_the_trivial_ladder() -> None:
    escalera = escalera_de(Decimal("7.25"), ())
    assert escalera == (Tramo(desde=Decimal("0"), hasta=None, tasa_nominal=Decimal("7.25")),)


def test_the_headline_rate_must_match_the_first_tier() -> None:
    """`Tasa.tasa_nominal` es SIEMPRE el tramo 1; una discrepancia es dato corrupto."""
    with pytest.raises(ValueError, match="incoherente"):
        escalera_de(Decimal("12.00"), OPENBANK)


def test_escalera_de_returns_the_tiers_sorted() -> None:
    assert escalera_de(Decimal("13.00"), (OPENBANK[1], OPENBANK[0])) == OPENBANK
