"""Tests de la cobertura de seguro de depósitos."""

from __future__ import annotations

from decimal import Decimal

import pytest

from domain.enums import SEGURO_POR_CATEGORIA, CategoriaInstitucion, TipoSeguro
from metrics.coverage import (
    LIMITE_IPAB_UDIS,
    LIMITE_PROSOFIPO_UDIS,
    cobertura_mxn,
    resolver_cobertura,
)

#: Valor real de la UDI del 2026-07-25 (serie SP68257 del SIE de Banxico).
UDI = Decimal("8.791497")


def test_limits_are_defined_in_udis_not_in_pesos() -> None:
    """§16: guardarlos en pesos congelaría un número que cambia a diario."""
    assert LIMITE_IPAB_UDIS == Decimal("400000")
    assert LIMITE_PROSOFIPO_UDIS == Decimal("25000")


def test_bank_coverage_at_the_current_udi() -> None:
    assert cobertura_mxn(TipoSeguro.IPAB, UDI) == Decimal("3516598.80")


def test_sofipo_coverage_at_the_current_udi() -> None:
    assert cobertura_mxn(TipoSeguro.PROSOFIPO, UDI) == Decimal("219787.43")


def test_sovereign_debt_has_no_limit() -> None:
    """`None` significa sin límite, no "desconocido"."""
    assert cobertura_mxn(TipoSeguro.SOBERANO, UDI) is None
    assert resolver_cobertura(TipoSeguro.SOBERANO, UDI).sin_limite is True


def test_ifpe_has_zero_coverage() -> None:
    """Distinto de "sin límite": es lo contrario."""
    cobertura = resolver_cobertura(TipoSeguro.NINGUNO, UDI)
    assert cobertura.limite_mxn == Decimal("0.00")
    assert cobertura.sin_cobertura is True
    assert cobertura.sin_limite is False


def test_coverage_follows_the_udi() -> None:
    """Si la UDI sube, la cobertura en pesos sube con ella."""
    baja = cobertura_mxn(TipoSeguro.PROSOFIPO, Decimal("8.0"))
    alta = cobertura_mxn(TipoSeguro.PROSOFIPO, Decimal("9.0"))
    assert baja is not None and alta is not None and alta > baja


def test_a_non_positive_udi_is_rejected() -> None:
    """Sin UDI válida no se inventa una cobertura."""
    for valor in ("0", "-1"):
        with pytest.raises(ValueError, match="positivo"):
            cobertura_mxn(TipoSeguro.IPAB, Decimal(valor))


def test_bank_coverage_is_sixteen_times_the_sofipo_one() -> None:
    """La diferencia que hace que confundir la figura sea grave.

    Es el error que cometen los comparadores que siguen listando a Nu como
    SOFIPO: muestran 220 mil de cobertura donde hay 3.5 millones.
    """
    assert LIMITE_IPAB_UDIS / LIMITE_PROSOFIPO_UDIS == 16

    ipab = cobertura_mxn(TipoSeguro.IPAB, UDI)
    prosofipo = cobertura_mxn(TipoSeguro.PROSOFIPO, UDI)
    assert ipab is not None and prosofipo is not None
    # En pesos la razón no es exacta por el redondeo a centavos de cada límite.
    assert abs(ipab / prosofipo - 16) < Decimal("0.001")
    assert ipab - prosofipo > Decimal("3000000")


@pytest.mark.parametrize("categoria", list(CategoriaInstitucion))
def test_every_category_resolves_to_a_coverage(categoria: CategoriaInstitucion) -> None:
    cobertura = resolver_cobertura(SEGURO_POR_CATEGORIA[categoria], UDI)
    assert cobertura.tipo is SEGURO_POR_CATEGORIA[categoria]


# ─── Exposición del ahorrador ─────────────────────────────────


def test_amount_within_the_limit_is_fully_covered() -> None:
    cobertura = resolver_cobertura(TipoSeguro.PROSOFIPO, UDI)
    assert cobertura.cubre(Decimal("200000")) is True
    assert cobertura.monto_expuesto(Decimal("200000")) == Decimal("0.00")


def test_amount_above_the_limit_is_partially_exposed() -> None:
    cobertura = resolver_cobertura(TipoSeguro.PROSOFIPO, UDI)
    assert cobertura.cubre(Decimal("300000")) is False
    assert cobertura.monto_expuesto(Decimal("300000")) == Decimal("80212.57")


def test_sovereign_debt_covers_any_amount() -> None:
    cobertura = resolver_cobertura(TipoSeguro.SOBERANO, UDI)
    assert cobertura.cubre(Decimal("50000000")) is True
    assert cobertura.monto_expuesto(Decimal("50000000")) == Decimal("0.00")


def test_ifpe_exposes_the_entire_amount() -> None:
    cobertura = resolver_cobertura(TipoSeguro.NINGUNO, UDI)
    assert cobertura.cubre(Decimal("1000")) is False
    assert cobertura.monto_expuesto(Decimal("1000")) == Decimal("1000.00")
