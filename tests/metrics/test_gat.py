"""Tests de la GAT y su equivalente calculado."""

from __future__ import annotations

from decimal import Decimal

import pytest

from metrics.gat import OrigenGat, gat_equivalente, gat_inconsistente, resolver_gat


def test_published_gat_wins_over_the_computed_one() -> None:
    """La GAT regulada existe: el comparador la centraliza, no la reinventa."""
    resultado = resolver_gat(
        Decimal("8.0"),
        Decimal("3.37"),
        gat_publicada_nominal=Decimal("7.85"),
        gat_publicada_real=Decimal("4.30"),
    )

    assert resultado.origen is OrigenGat.PUBLICADA
    assert resultado.es_calculada is False
    assert resultado.nominal == Decimal("7.8500")
    assert resultado.real == Decimal("4.3000")


def test_missing_real_gat_is_completed_from_inflation() -> None:
    """Sigue siendo PUBLICADA: el número de partida es el regulado."""
    resultado = resolver_gat(
        Decimal("8.0"),
        Decimal("3.37"),
        gat_publicada_nominal=Decimal("7.85"),
    )

    assert resultado.origen is OrigenGat.PUBLICADA
    assert resultado.real == Decimal("4.4800")


def test_instruments_without_published_gat_get_an_equivalent() -> None:
    """§4.4: CETES no publica GAT, pero tiene que caber en la misma columna.

    La GAT es antes de impuestos por definición, así que sin comisiones el
    equivalente **es** la nominal — comparable con cualquier GAT publicada.
    El ISR vive en la TEN, que es su propia columna.
    """
    resultado = resolver_gat(Decimal("6.18"), Decimal("3.37"))

    assert resultado.origen is OrigenGat.CALCULADA
    assert resultado.es_calculada is True
    assert resultado.nominal == Decimal("6.1800")
    assert resultado.real == Decimal("2.8100")


def test_commissions_reduce_the_equivalent() -> None:
    """BONDDIA cobra comisión de administración sobre el rendimiento bruto."""
    sin_comision = gat_equivalente(Decimal("6.42"), Decimal("3.37"))
    con_comision = gat_equivalente(
        Decimal("6.42"),
        Decimal("3.37"),
        comisiones_anuales_pct=Decimal("0.25"),
    )

    assert sin_comision.nominal == Decimal("6.4200")
    assert con_comision.nominal == Decimal("6.1700")
    assert con_comision.real == Decimal("2.8000")


def test_real_gat_is_always_nominal_minus_inflation() -> None:
    for inflacion in ("0.0", "3.37", "8.0", "12.5"):
        resultado = gat_equivalente(Decimal("6.18"), Decimal(inflacion))
        assert resultado.real == resultado.nominal - Decimal(inflacion)


def test_real_gat_can_be_negative() -> None:
    """Con inflación por encima del rendimiento, el ahorro pierde valor."""
    resultado = gat_equivalente(Decimal("4.0"), Decimal("6.0"))
    assert resultado.real < 0


# ─── Bandera de GAT inconsistente (§5.2) ──────────────────────

UMBRAL = Decimal("1.5")


def test_consistent_gat_raises_no_flag() -> None:
    assert gat_inconsistente(Decimal("7.85"), Decimal("8.0"), UMBRAL) is False


def test_gat_far_below_the_nominal_rate_is_suspicious() -> None:
    """Sugiere comisiones no evidentes o condiciones restrictivas."""
    assert gat_inconsistente(Decimal("5.0"), Decimal("8.0"), UMBRAL) is True


def test_gat_far_above_the_nominal_rate_is_equally_suspicious() -> None:
    """No debería poder estar por encima: la GAT incluye todos los costos."""
    assert gat_inconsistente(Decimal("11.0"), Decimal("8.0"), UMBRAL) is True


def test_difference_exactly_at_the_threshold_does_not_flag() -> None:
    """El umbral es exclusivo: se marca por encima, no al llegar."""
    assert gat_inconsistente(Decimal("6.5"), Decimal("8.0"), UMBRAL) is False
    assert gat_inconsistente(Decimal("6.49"), Decimal("8.0"), UMBRAL) is True


def test_absent_gat_cannot_be_inconsistent() -> None:
    """No publicarla no es una irregularidad: muchos instrumentos no la tienen."""
    assert gat_inconsistente(None, Decimal("8.0"), UMBRAL) is False


@pytest.mark.parametrize("umbral", ["0.5", "1.5", "3.0"])
def test_threshold_is_injected_not_hardcoded(umbral: str) -> None:
    """Viene de ConfigStore; el módulo no puede fijarlo."""
    diferencia_2pp = gat_inconsistente(Decimal("6.0"), Decimal("8.0"), Decimal(umbral))
    assert diferencia_2pp is (Decimal(umbral) < Decimal("2"))
