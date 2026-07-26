"""Tests de la Tasa Efectiva Neta."""

from __future__ import annotations

from decimal import Decimal

import pytest

from domain.enums import TipoInstrumento
from domain.models import ParametrosFiscales
from metrics.ten import ten, ten_desde_bruto_y_neto


def test_foundation_example_from_section_4_5(fiscal_2025: ParametrosFiscales) -> None:
    """Ejemplo obligatorio: 7.5% nominal → TEN ≈ 7.0%.

    Se usa el ejercicio 2025 porque es la tasa de retención con la que se
    escribió el foundation (0.50%). Con la de 2026 el mismo caso da 6.6%, y eso
    lo cubre el test siguiente.
    """
    assert ten(Decimal("7.5"), TipoInstrumento.CETES, fiscal_2025) == Decimal("7.0000")


def test_the_same_case_under_the_2026_rate(fiscal_2026: ParametrosFiscales) -> None:
    """El aumento de la LIF 2026 se lleva 0.4 puntos de rendimiento neto."""
    assert ten(Decimal("7.5"), TipoInstrumento.CETES, fiscal_2026) == Decimal("6.6000")


def test_ten_is_annualized_and_therefore_term_independent(
    fiscal_2026: ParametrosFiscales,
) -> None:
    """Un CETE a 28 días y otro a 364 con la misma nominal tienen la misma TEN."""
    valor = ten(Decimal("6.18"), TipoInstrumento.CETES, fiscal_2026)
    assert valor == Decimal("5.2800")
    # La firma no admite plazo justamente porque no interviene.
    assert ten(Decimal("6.18"), TipoInstrumento.CETES, fiscal_2026) == valor


def test_ten_can_be_negative(fiscal_2026: ParametrosFiscales) -> None:
    """No se recorta a cero: ocultarlo sería la letra chica que §11 prohíbe."""
    assert ten(Decimal("0.50"), TipoInstrumento.CETES, fiscal_2026) == Decimal("-0.4000")


def test_ten_is_never_above_the_nominal_rate(fiscal_2026: ParametrosFiscales) -> None:
    for nominal in ("0.0", "3.5", "6.18", "15.0", "99.9"):
        tasa = Decimal(nominal)
        assert ten(tasa, TipoInstrumento.CETES, fiscal_2026) <= tasa


@pytest.mark.parametrize(
    "instrumento",
    [
        TipoInstrumento.CETES,
        TipoInstrumento.DEPOSITO_SOFIPO,
        TipoInstrumento.DEPOSITO_BANCARIO,
        TipoInstrumento.PRLV,
        TipoInstrumento.BONDDIA,
    ],
)
def test_capital_based_instruments_share_the_same_deduction(
    instrumento: TipoInstrumento, fiscal_2026: ParametrosFiscales
) -> None:
    """Comparación justa (§11): la misma métrica para todos los instrumentos.

    Con base capital, la resta es idéntica, así que el orden por TEN coincide
    con el orden por tasa nominal — y es correcto que así sea.
    """
    assert ten(Decimal("8.0"), instrumento, fiscal_2026) == Decimal("7.1000")


def test_higher_nominal_yields_higher_ten(fiscal_2026: ParametrosFiscales) -> None:
    """Monotonía: más tasa nominal ⇒ más TEN, ceteris paribus."""
    anterior = ten(Decimal("1.0"), TipoInstrumento.CETES, fiscal_2026)
    for nominal in ("2.0", "5.0", "6.18", "12.0"):
        actual = ten(Decimal(nominal), TipoInstrumento.CETES, fiscal_2026)
        assert actual > anterior
        anterior = actual


def test_implicit_ten_matches_the_declared_one(fiscal_2026: ParametrosFiscales) -> None:
    """La TEN implícita en los pesos coincide con la calculada desde la tasa."""
    monto = Decimal("100000")
    # 6.18% nominal a 360 días con retención 0.90% → neto 5,280.
    neto = Decimal("5280.00")

    implicita = ten_desde_bruto_y_neto(monto, neto, 360)
    declarada = ten(Decimal("6.18"), TipoInstrumento.CETES, fiscal_2026)

    assert implicita == declarada == Decimal("5.2800")


def test_implicit_ten_annualizes_short_terms() -> None:
    """Un rendimiento de 28 días se expresa como tasa anual."""
    # 100,000 al 5.28% anual durante 28 días = 410.67 (base 360).
    assert ten_desde_bruto_y_neto(Decimal("100000"), Decimal("410.67"), 28) == Decimal("5.2800")


def test_implicit_ten_rejects_a_non_positive_amount() -> None:
    with pytest.raises(ValueError, match="positivo"):
        ten_desde_bruto_y_neto(Decimal("0"), Decimal("100"), 28)


def test_ten_returns_decimal(fiscal_2026: ParametrosFiscales) -> None:
    assert isinstance(ten(Decimal("6.18"), TipoInstrumento.CETES, fiscal_2026), Decimal)
