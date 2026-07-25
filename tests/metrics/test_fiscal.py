"""Tests del tratamiento fiscal."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from domain.enums import TipoInstrumento
from domain.models import ParametrosFiscales
from metrics.fiscal import (
    BASE_ANUAL_DIAS,
    TRATAMIENTO_POR_INSTRUMENTO,
    BaseRetencion,
    TratamientoFiscal,
    factor_plazo,
    nota_fiscal,
    rendimiento_bruto,
    retencion_isr,
    tasa_retencion_efectiva_anual,
    tratamiento,
)


def test_every_instrument_has_a_declared_treatment() -> None:
    """Un instrumento sin tratamiento no puede calcular en silencio."""
    assert set(TRATAMIENTO_POR_INSTRUMENTO) == set(TipoInstrumento)


def test_unknown_instrument_fails_loudly() -> None:
    with pytest.raises(KeyError, match="tratamiento fiscal"):
        tratamiento("INVENTADO")  # type: ignore[arg-type]


def test_day_count_basis_is_360() -> None:
    """Convención del mercado de dinero mexicano y de la regla de la RMF."""
    assert BASE_ANUAL_DIAS == Decimal("360")
    assert factor_plazo(360) == Decimal("1")
    assert factor_plazo(180) == Decimal("0.5")


def test_non_positive_term_is_rejected() -> None:
    for plazo in (0, -1):
        with pytest.raises(ValueError, match="positivo"):
            factor_plazo(plazo)


# ─── Retención sobre capital ──────────────────────────────────


def test_withholding_is_computed_on_capital_not_on_the_gain(
    fiscal_2026: ParametrosFiscales,
) -> None:
    """El punto central del art. 54: la base es el capital.

    Dos instrumentos con el mismo monto y distinta tasa retienen lo mismo.
    """
    baja = retencion_isr(
        TipoInstrumento.CETES, Decimal("100000"), Decimal("3.0"), 360, fiscal_2026
    )
    alta = retencion_isr(
        TipoInstrumento.CETES, Decimal("100000"), Decimal("15.0"), 360, fiscal_2026
    )

    assert baja == alta == Decimal("900.00")


def test_withholding_is_prorated_by_term(fiscal_2026: ParametrosFiscales) -> None:
    anual = retencion_isr(
        TipoInstrumento.CETES, Decimal("100000"), Decimal("6.18"), 360, fiscal_2026
    )
    medio = retencion_isr(
        TipoInstrumento.CETES, Decimal("100000"), Decimal("6.18"), 180, fiscal_2026
    )

    assert anual == Decimal("900.00")
    assert medio == Decimal("450.00")


def test_withholding_can_exceed_the_yield(fiscal_2026: ParametrosFiscales) -> None:
    """Consecuencia contraintuitiva que el producto existe para hacer visible.

    Con una tasa nominal por debajo de la de retención, la ganancia neta es
    negativa: el ahorrador pierde dinero nominal, no sólo poder adquisitivo.
    """
    monto = Decimal("100000")
    bruto = rendimiento_bruto(monto, Decimal("0.50"), 360)
    isr = retencion_isr(TipoInstrumento.CETES, monto, Decimal("0.50"), 360, fiscal_2026)

    assert bruto == Decimal("500.00")
    assert isr == Decimal("900.00")
    assert bruto - isr < 0


@pytest.mark.parametrize(
    "instrumento",
    [
        TipoInstrumento.CETES,
        TipoInstrumento.BONDDIA,
        TipoInstrumento.BONOS_M,
        TipoInstrumento.PRLV,
        TipoInstrumento.DEPOSITO_SOFIPO,
        TipoInstrumento.DEPOSITO_BANCARIO,
    ],
)
def test_section_4_2_instruments_withhold_on_capital(instrumento: TipoInstrumento) -> None:
    assert tratamiento(instrumento).base is BaseRetencion.CAPITAL


def test_debt_funds_withhold_on_capital_diverging_from_the_foundation() -> None:
    """Divergencia deliberada y verificada contra la ley.

    §4.2 dice "retención sobre ganancia" para fondos de deuda. El artículo 87
    de la LISR releva al fondo de retener y remite al régimen del artículo 54,
    que es sobre capital; la regla de la RMF lo confirma con su fórmula diaria.
    """
    trato = tratamiento(TipoInstrumento.FONDO_DEUDA)
    assert trato.base is BaseRetencion.CAPITAL
    assert "art. 87" in trato.detalle


def test_udibonos_defer_the_inflation_adjustment() -> None:
    """La diferencia es de momento, no de tasa."""
    trato = tratamiento(TipoInstrumento.UDIBONOS)
    assert trato.ajuste_inflacionario_diferido is True
    assert trato.base is BaseRetencion.CAPITAL

    assert tratamiento(TipoInstrumento.CETES).ajuste_inflacionario_diferido is False


# ─── Retención sobre ganancia ─────────────────────────────────


def test_gain_based_withholding_scales_with_the_yield(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Base GANANCIA: aquí sí importa cuánto rindió.

    Ningún instrumento del catálogo la usa hoy, así que se prueba mapeando uno
    temporalmente: el motor debe soportar la base que §4.2 describe, aunque la
    ley la haya dejado sin uso en este catálogo.
    """
    params = ParametrosFiscales(
        anio=2026,
        tasa_retencion_capital=Decimal("0.90"),
        tasa_retencion_ganancia=Decimal("20.0"),
        vigente_desde=date(2026, 1, 1),
    )
    monkeypatch.setitem(
        TRATAMIENTO_POR_INSTRUMENTO,
        TipoInstrumento.FONDO_DEUDA,
        TratamientoFiscal(BaseRetencion.GANANCIA),
    )

    isr = retencion_isr(
        TipoInstrumento.FONDO_DEUDA, Decimal("100000"), Decimal("10.0"), 360, params
    )
    # 10% de 100,000 = 10,000 de interés; 20% de eso = 2,000.
    assert isr == Decimal("2000.00")


# ─── Tasa efectiva de retención ───────────────────────────────


def test_effective_withholding_rate_equals_the_statutory_rate_on_capital(
    fiscal_2026: ParametrosFiscales,
) -> None:
    resta = tasa_retencion_efectiva_anual(TipoInstrumento.CETES, Decimal("6.18"), fiscal_2026)
    assert resta == Decimal("0.9000")


def test_effective_withholding_rate_follows_the_fiscal_year(
    fiscal_2025: ParametrosFiscales, fiscal_2026: ParametrosFiscales
) -> None:
    """El cambio de la LIF 2026 se refleja sin tocar código."""
    de_2025 = tasa_retencion_efectiva_anual(TipoInstrumento.CETES, Decimal("7.5"), fiscal_2025)
    de_2026 = tasa_retencion_efectiva_anual(TipoInstrumento.CETES, Decimal("7.5"), fiscal_2026)

    assert de_2025 == Decimal("0.5000")
    assert de_2026 == Decimal("0.9000")


# ─── Nota fiscal ──────────────────────────────────────────────


def test_fiscal_note_states_the_rate_and_its_effective_date(
    fiscal_2026: ParametrosFiscales,
) -> None:
    """§6: qué retención se aplicó y cuándo se actualizó."""
    nota = nota_fiscal(TipoInstrumento.CETES, fiscal_2026)

    assert "0.90" in nota
    assert "2026-01-01" in nota
    assert "no sobre la ganancia" in nota


def test_fiscal_note_explains_the_provisional_nature(
    fiscal_2026: ParametrosFiscales,
) -> None:
    """§11, honestidad fiscal: no basta con restar, hay que explicar."""
    nota = nota_fiscal(TipoInstrumento.CETES, fiscal_2026)
    assert "acreditable" in nota
    assert "declaración anual" in nota


def test_fiscal_note_carries_the_instrument_specific_detail(
    fiscal_2026: ParametrosFiscales,
) -> None:
    assert "vencimiento" in nota_fiscal(TipoInstrumento.UDIBONOS, fiscal_2026)
    assert "IPAB" in nota_fiscal(TipoInstrumento.MONEDERO_ELECTRONICO, fiscal_2026)


def test_gross_yield_is_prorated_and_rounded_to_cents() -> None:
    assert rendimiento_bruto(Decimal("100000"), Decimal("6.18"), 360) == Decimal("6180.00")
    assert rendimiento_bruto(Decimal("100000"), Decimal("6.18"), 28) == Decimal("480.67")


def test_no_result_is_a_float(fiscal_2026: ParametrosFiscales) -> None:
    """Ninguna función del motor puede devolver float."""
    params = fiscal_2026
    resultados = [
        factor_plazo(28),
        rendimiento_bruto(Decimal("1000"), Decimal("7"), 28),
        retencion_isr(TipoInstrumento.CETES, Decimal("1000"), Decimal("7"), 28, params),
        tasa_retencion_efectiva_anual(TipoInstrumento.CETES, Decimal("7"), params),
    ]
    for valor in resultados:
        assert isinstance(valor, Decimal)
