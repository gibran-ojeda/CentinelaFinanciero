"""Tests de la ganancia real y de la cascada de la calculadora.

Contiene los ejemplos numéricos obligatorios del foundation (§4.5 y §6).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from domain.enums import TipoInstrumento
from domain.models import ParametrosFiscales
from metrics.real import desglose_cascada, efecto_inflacion, ganancia_real_anual

#: Inflación INPC anual real a junio de 2026 (calculada de la serie SP1).
INFLACION_ACTUAL = Decimal("3.37")


# ─── Ejemplos obligatorios del foundation ─────────────────────


def test_section_4_5_example(fiscal_2025: ParametrosFiscales) -> None:
    """§4.5 literal: $100,000 al 7.5% → neta $7,000 → real ~$2,500.

    El documento lo enuncia como "CETE 28d", pero sus importes son los
    anualizados (7,000 sobre 100,000 = 7%). Se reproduce con el plazo de un año
    completo, que es lo que esos números representan, y con la retención de
    2025, que es la vigente cuando se escribió.
    """
    resultado = desglose_cascada(
        monto=Decimal("100000"),
        tasa_nominal=Decimal("7.5"),
        instrumento=TipoInstrumento.CETES,
        plazo_dias=360,
        inflacion_anual=Decimal("4.5"),
        params=fiscal_2025,
    )

    assert resultado.rendimiento_bruto == Decimal("7500.00")
    assert resultado.isr_retenido == Decimal("500.00")
    assert resultado.rendimiento_neto == Decimal("7000.00")
    assert resultado.ten == Decimal("7.0000")
    assert resultado.efecto_inflacion == Decimal("4500.00")
    assert resultado.ganancia_real == Decimal("2500.00")


def test_section_6_narrative(fiscal_2025: ParametrosFiscales) -> None:
    """§6 literal: "de $1,000 brutos: $50 impuestos, $450 inflación, $500 real".

    Se despeja el caso que produce esos números: $10,000 al 10% anual, con
    retención de 0.50% e inflación de 4.5%.
    """
    resultado = desglose_cascada(
        monto=Decimal("10000"),
        tasa_nominal=Decimal("10.0"),
        instrumento=TipoInstrumento.CETES,
        plazo_dias=360,
        inflacion_anual=Decimal("4.5"),
        params=fiscal_2025,
    )

    assert resultado.rendimiento_bruto == Decimal("1000.00")
    assert resultado.isr_retenido == Decimal("50.00")
    assert resultado.efecto_inflacion == Decimal("450.00")
    assert resultado.ganancia_real == Decimal("500.00")


def test_section_4_5_formula_matches_the_cascade(fiscal_2025: ParametrosFiscales) -> None:
    """La fórmula literal de §4.5 y la cascada dan el mismo número."""
    directo = ganancia_real_anual(
        Decimal("100000"),
        Decimal("7.5"),
        TipoInstrumento.CETES,
        Decimal("4.5"),
        fiscal_2025,
    )
    por_cascada = desglose_cascada(
        Decimal("100000"),
        Decimal("7.5"),
        TipoInstrumento.CETES,
        360,
        Decimal("4.5"),
        fiscal_2025,
    ).ganancia_real

    assert directo == por_cascada == Decimal("2500.00")


# ─── Invariantes de la cascada ────────────────────────────────


@pytest.mark.parametrize("monto", ["1000", "10000", "100000", "1234567.89"])
@pytest.mark.parametrize("tasa", ["0.5", "3.33", "6.18", "15.75"])
@pytest.mark.parametrize("plazo", [1, 28, 91, 182, 360, 730])
def test_cascade_always_adds_up_exactly(
    monto: str, tasa: str, plazo: int, fiscal_2026: ParametrosFiscales
) -> None:
    """Identidad exacta, no aproximada: el usuario va a sumar lo que ve."""
    resultado = desglose_cascada(
        Decimal(monto),
        Decimal(tasa),
        TipoInstrumento.CETES,
        plazo,
        INFLACION_ACTUAL,
        fiscal_2026,
    )

    assert (
        resultado.rendimiento_bruto
        == resultado.isr_retenido + resultado.efecto_inflacion + resultado.ganancia_real
    )
    assert resultado.rendimiento_neto == resultado.rendimiento_bruto - resultado.isr_retenido
    assert resultado.ganancia_real == resultado.rendimiento_neto - resultado.efecto_inflacion


def test_real_gain_is_often_negative_at_current_rates(
    fiscal_2026: ParametrosFiscales,
) -> None:
    """El hallazgo que justifica el producto, con datos reales de hoy.

    CETES a 28 días al 6.18% con la retención de 2026 y la inflación de junio:
    el ahorrador gana en pesos pero apenas conserva poder adquisitivo.
    """
    resultado = desglose_cascada(
        Decimal("100000"),
        Decimal("6.18"),
        TipoInstrumento.CETES,
        360,
        INFLACION_ACTUAL,
        fiscal_2026,
    )

    assert resultado.rendimiento_bruto == Decimal("6180.00")
    assert resultado.rendimiento_neto == Decimal("5280.00")
    assert resultado.ganancia_real == Decimal("1910.00")
    # Menos de un tercio de lo que sugiere la tasa nominal.
    assert resultado.ganancia_real < resultado.rendimiento_bruto / 3


def test_high_inflation_turns_the_real_gain_negative(
    fiscal_2026: ParametrosFiscales,
) -> None:
    resultado = desglose_cascada(
        Decimal("100000"),
        Decimal("6.18"),
        TipoInstrumento.CETES,
        360,
        Decimal("8.0"),
        fiscal_2026,
    )
    assert resultado.ganancia_real < 0


def test_more_inflation_means_less_real_gain(fiscal_2026: ParametrosFiscales) -> None:
    """Monotonía."""
    anterior = None
    for inflacion in ("0.0", "2.0", "4.0", "6.0", "10.0"):
        actual = desglose_cascada(
            Decimal("100000"),
            Decimal("6.18"),
            TipoInstrumento.CETES,
            360,
            Decimal(inflacion),
            fiscal_2026,
        ).ganancia_real
        if anterior is not None:
            assert actual < anterior
        anterior = actual


def test_longer_term_yields_proportionally_more(fiscal_2026: ParametrosFiscales) -> None:
    corto = desglose_cascada(
        Decimal("100000"),
        Decimal("6.18"),
        TipoInstrumento.CETES,
        180,
        INFLACION_ACTUAL,
        fiscal_2026,
    )
    largo = desglose_cascada(
        Decimal("100000"),
        Decimal("6.18"),
        TipoInstrumento.CETES,
        360,
        INFLACION_ACTUAL,
        fiscal_2026,
    )

    assert largo.rendimiento_bruto == corto.rendimiento_bruto * 2
    assert largo.ganancia_real == corto.ganancia_real * 2


def test_ten_is_the_same_regardless_of_term(fiscal_2026: ParametrosFiscales) -> None:
    """La cascada escala con el plazo; la TEN no."""
    plazos = [28, 91, 182, 360]
    valores = {
        desglose_cascada(
            Decimal("100000"),
            Decimal("6.18"),
            TipoInstrumento.CETES,
            plazo,
            INFLACION_ACTUAL,
            fiscal_2026,
        ).ten
        for plazo in plazos
    }
    assert valores == {Decimal("5.2800")}


# ─── Nota fiscal y validación ─────────────────────────────────


def test_every_breakdown_carries_its_fiscal_note(fiscal_2026: ParametrosFiscales) -> None:
    """§6: la calculadora nunca muestra números sin decir qué retención aplicó."""
    resultado = desglose_cascada(
        Decimal("100000"),
        Decimal("6.18"),
        TipoInstrumento.CETES,
        360,
        INFLACION_ACTUAL,
        fiscal_2026,
    )

    assert "0.90" in resultado.nota_fiscal
    assert "2026-01-01" in resultado.nota_fiscal


def test_breakdown_echoes_its_inputs(fiscal_2026: ParametrosFiscales) -> None:
    """El resultado es autocontenido: se puede auditar sin la llamada."""
    resultado = desglose_cascada(
        Decimal("50000"),
        Decimal("7.19"),
        TipoInstrumento.DEPOSITO_SOFIPO,
        91,
        INFLACION_ACTUAL,
        fiscal_2026,
    )

    assert resultado.monto_invertido == Decimal("50000.00")
    assert resultado.tasa_nominal == Decimal("7.19")
    assert resultado.plazo_dias == 91
    assert resultado.inflacion_anual == INFLACION_ACTUAL


def test_non_positive_amount_is_rejected(fiscal_2026: ParametrosFiscales) -> None:
    for monto in ("0", "-100"):
        with pytest.raises(ValueError, match="positivo"):
            desglose_cascada(
                Decimal(monto),
                Decimal("6.18"),
                TipoInstrumento.CETES,
                360,
                INFLACION_ACTUAL,
                fiscal_2026,
            )


def test_non_positive_term_is_rejected(fiscal_2026: ParametrosFiscales) -> None:
    with pytest.raises(ValueError, match="positivo"):
        desglose_cascada(
            Decimal("100000"),
            Decimal("6.18"),
            TipoInstrumento.CETES,
            0,
            INFLACION_ACTUAL,
            fiscal_2026,
        )


def test_inflation_effect_is_prorated_by_term() -> None:
    assert efecto_inflacion(Decimal("100000"), Decimal("3.37"), 360) == Decimal("3370.00")
    assert efecto_inflacion(Decimal("100000"), Decimal("3.37"), 180) == Decimal("1685.00")


def test_every_amount_is_decimal(fiscal_2026: ParametrosFiscales) -> None:
    resultado = desglose_cascada(
        Decimal("100000"),
        Decimal("6.18"),
        TipoInstrumento.CETES,
        360,
        INFLACION_ACTUAL,
        fiscal_2026,
    )
    for campo in (
        resultado.monto_invertido,
        resultado.rendimiento_bruto,
        resultado.isr_retenido,
        resultado.rendimiento_neto,
        resultado.efecto_inflacion,
        resultado.ganancia_real,
        resultado.ten,
    ):
        assert isinstance(campo, Decimal)
