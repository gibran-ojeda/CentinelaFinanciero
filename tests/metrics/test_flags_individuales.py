"""Matriz de banderas individuales (§5.1).

Un caso por celda de cada tabla del foundation: sano, atención y alerta.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from domain.enums import NivelCapitalizacion, Severidad, TipoBandera, TipoSeguro
from domain.models import IndicadoresInstitucion, UmbralesBanderas
from metrics.flags import (
    evaluar_apalancamiento,
    evaluar_cobertura_cartera,
    evaluar_cobertura_seguro,
    evaluar_icap,
    evaluar_imor,
    evaluar_individuales,
    evaluar_nicap,
)

PERIODO = date(2026, 3, 31)


@pytest.fixture
def umbrales() -> UmbralesBanderas:
    return UmbralesBanderas()


def _indicadores(**campos: object) -> IndicadoresInstitucion:
    return IndicadoresInstitucion(institucion_id=1, periodo=PERIODO, **campos)  # type: ignore[arg-type]


# ─── IMOR: < 3% sano / 3-6% 🟡 / > 6% 🔴 ──────────────────────


@pytest.mark.parametrize("imor", ["0.0", "1.5", "2.99"])
def test_healthy_imor_raises_no_flag(imor: str, umbrales: UmbralesBanderas) -> None:
    assert evaluar_imor(_indicadores(imor=Decimal(imor)), umbrales) is None


@pytest.mark.parametrize("imor", ["3.0", "4.5", "6.0"])
def test_imor_in_the_attention_range_is_yellow(imor: str, umbrales: UmbralesBanderas) -> None:
    bandera = evaluar_imor(_indicadores(imor=Decimal(imor)), umbrales)
    assert bandera is not None
    assert bandera.severidad is Severidad.AMARILLA
    assert bandera.tipo is TipoBandera.IMOR


@pytest.mark.parametrize("imor", ["6.01", "9.0", "25.0"])
def test_imor_above_the_alert_threshold_is_red(imor: str, umbrales: UmbralesBanderas) -> None:
    bandera = evaluar_imor(_indicadores(imor=Decimal(imor)), umbrales)
    assert bandera is not None
    assert bandera.severidad is Severidad.ROJA


def test_imor_flag_explains_itself_in_plain_language(umbrales: UmbralesBanderas) -> None:
    """§10: son señales orientativas, así que hay que decir qué significan.

    Y decirlo del indicador, no de la institución: el motivo explica qué
    implica una morosidad en ese rango, sin afirmar como hecho un estado de
    la institución deducido de un solo cociente. Ver los criterios de
    redacción que cierran D5.
    """
    bandera = evaluar_imor(_indicadores(imor=Decimal("9.0")), umbrales)
    assert bandera is not None
    assert "9.0%" in bandera.motivo
    assert "cobrar lo prestado" in bandera.motivo
    assert "La institución tiene" not in bandera.motivo


def test_flags_carry_the_period_of_the_underlying_data(umbrales: UmbralesBanderas) -> None:
    """La CNBV publica con 1-3 meses de rezago: una bandera sin fecha engaña."""
    bandera = evaluar_imor(_indicadores(imor=Decimal("9.0")), umbrales)
    assert bandera is not None
    assert bandera.periodo_dato == PERIODO


# ─── Cobertura de cartera: > 100% / 70-100% 🟡 / < 70% 🔴 ─────


@pytest.mark.parametrize("icor", ["100.0", "150.0", "300.0"])
def test_adequate_coverage_raises_no_flag(icor: str, umbrales: UmbralesBanderas) -> None:
    assert evaluar_cobertura_cartera(_indicadores(icor=Decimal(icor)), umbrales) is None


@pytest.mark.parametrize("icor", ["70.0", "85.0", "99.99"])
def test_partial_coverage_is_yellow(icor: str, umbrales: UmbralesBanderas) -> None:
    bandera = evaluar_cobertura_cartera(_indicadores(icor=Decimal(icor)), umbrales)
    assert bandera is not None
    assert bandera.severidad is Severidad.AMARILLA


@pytest.mark.parametrize("icor", ["69.99", "40.0", "0.0"])
def test_insufficient_coverage_is_red(icor: str, umbrales: UmbralesBanderas) -> None:
    bandera = evaluar_cobertura_cartera(_indicadores(icor=Decimal(icor)), umbrales)
    assert bandera is not None
    assert bandera.severidad is Severidad.ROJA


# ─── ICAP: > 15% / 10.5-15% 🟡 / < 10.5% 🔴 ───────────────────


@pytest.mark.parametrize("icap", ["15.0", "22.0", "80.0"])
def test_healthy_icap_raises_no_flag(icap: str, umbrales: UmbralesBanderas) -> None:
    assert evaluar_icap(_indicadores(icap=Decimal(icap)), umbrales) is None


@pytest.mark.parametrize("icap", ["10.5", "12.0", "14.99"])
def test_icap_with_little_headroom_is_yellow(icap: str, umbrales: UmbralesBanderas) -> None:
    bandera = evaluar_icap(_indicadores(icap=Decimal(icap)), umbrales)
    assert bandera is not None
    assert bandera.severidad is Severidad.AMARILLA


@pytest.mark.parametrize("icap", ["10.49", "8.0", "0.0"])
def test_icap_below_the_regulatory_minimum_is_red(icap: str, umbrales: UmbralesBanderas) -> None:
    bandera = evaluar_icap(_indicadores(icap=Decimal(icap)), umbrales)
    assert bandera is not None
    assert bandera.severidad is Severidad.ROJA
    assert "mínimo regulatorio" in bandera.motivo


# ─── NICAP: N1 sin bandera / N2 🟡 / N3-N4 🔴 ─────────────────


def test_nicap_n1_raises_no_flag() -> None:
    assert evaluar_nicap(_indicadores(nicap_nivel=NivelCapitalizacion.N1)) is None


def test_nicap_n2_is_yellow() -> None:
    bandera = evaluar_nicap(_indicadores(nicap_nivel=NivelCapitalizacion.N2))
    assert bandera is not None
    assert bandera.severidad is Severidad.AMARILLA


@pytest.mark.parametrize("nivel", [NivelCapitalizacion.N3, NivelCapitalizacion.N4])
def test_nicap_n3_and_n4_are_red(nivel: NivelCapitalizacion) -> None:
    bandera = evaluar_nicap(_indicadores(nicap_nivel=nivel))
    assert bandera is not None
    assert bandera.severidad is Severidad.ROJA
    assert "medidas correctivas" in bandera.motivo


# ─── Apalancamiento ──────────────────────────────────────────


def test_moderate_leverage_raises_no_flag(umbrales: UmbralesBanderas) -> None:
    indicadores = _indicadores(pasivo_total=Decimal("500"), capital_contable=Decimal("100"))
    assert evaluar_apalancamiento(indicadores, umbrales) is None


def test_high_leverage_is_yellow(umbrales: UmbralesBanderas) -> None:
    indicadores = _indicadores(pasivo_total=Decimal("1500"), capital_contable=Decimal("100"))
    bandera = evaluar_apalancamiento(indicadores, umbrales)
    assert bandera is not None
    assert bandera.severidad is Severidad.AMARILLA
    assert "15.00" in bandera.motivo


# ─── Ausencia de dato ────────────────────────────────────────


def test_missing_indicators_produce_no_flags(umbrales: UmbralesBanderas) -> None:
    """Sin dato se calla; no se supone lo peor."""
    assert evaluar_individuales(_indicadores(), umbrales) == []


def test_only_the_available_indicators_are_evaluated(umbrales: UmbralesBanderas) -> None:
    """Un banco no trae NICAP y una SOFIPO sí: el motor tolera ambos."""
    solo_imor = evaluar_individuales(_indicadores(imor=Decimal("9.0")), umbrales)
    assert [b.tipo for b in solo_imor] == [TipoBandera.IMOR]


def test_all_individual_flags_can_coexist(umbrales: UmbralesBanderas) -> None:
    banderas = evaluar_individuales(
        _indicadores(
            imor=Decimal("9.0"),
            icor=Decimal("50.0"),
            icap=Decimal("8.0"),
            nicap_nivel=NivelCapitalizacion.N3,
            pasivo_total=Decimal("1500"),
            capital_contable=Decimal("100"),
        ),
        umbrales,
    )
    assert {b.tipo for b in banderas} == {
        TipoBandera.IMOR,
        TipoBandera.COBERTURA_CARTERA,
        TipoBandera.ICAP,
        TipoBandera.NICAP,
        TipoBandera.APALANCAMIENTO,
    }


def test_individual_flags_are_never_marked_composite(umbrales: UmbralesBanderas) -> None:
    banderas = evaluar_individuales(
        _indicadores(imor=Decimal("9.0"), icap=Decimal("8.0")), umbrales
    )
    assert all(b.compuesta is False for b in banderas)


# ─── Umbrales inyectados ─────────────────────────────────────


def test_thresholds_come_from_the_caller() -> None:
    """El módulo no importa ConfigStore: recibe el objeto y punto."""
    indicadores = _indicadores(imor=Decimal("4.0"))

    con_defaults = evaluar_imor(indicadores, UmbralesBanderas())
    con_umbral_alto = evaluar_imor(indicadores, UmbralesBanderas(imor_amarilla=Decimal("5.0")))

    assert con_defaults is not None
    assert con_umbral_alto is None


# ─── Cobertura de seguro (§5.3) ──────────────────────────────


def test_ifpe_always_carries_the_no_coverage_flag() -> None:
    """Permanente e informativa: depende de la figura, no de indicadores."""
    bandera = evaluar_cobertura_seguro(_indicadores(), TipoSeguro.NINGUNO)
    assert bandera is not None
    assert bandera.tipo is TipoBandera.SIN_COBERTURA
    assert bandera.severidad is Severidad.AMARILLA
    assert "fideicomiso segregado" in bandera.motivo


@pytest.mark.parametrize("seguro", [TipoSeguro.IPAB, TipoSeguro.PROSOFIPO, TipoSeguro.SOBERANO])
def test_covered_institutions_carry_no_such_flag(seguro: TipoSeguro) -> None:
    assert evaluar_cobertura_seguro(_indicadores(), seguro) is None
