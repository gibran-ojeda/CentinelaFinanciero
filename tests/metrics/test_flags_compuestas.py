"""Banderas compuestas y resolución de prioridad (§5.2)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from domain.enums import Severidad, TipoBandera, TipoSeguro
from domain.models import IndicadoresInstitucion, UmbralesBanderas
from metrics.flags import (
    evaluar_banderas,
    evaluar_gat_inconsistente,
    evaluar_no_recomendable,
    evaluar_red_flag_tasa,
    resolver_prioridad,
)

PERIODO = date(2026, 3, 31)


@pytest.fixture
def umbrales() -> UmbralesBanderas:
    return UmbralesBanderas()


def _indicadores(**campos: object) -> IndicadoresInstitucion:
    return IndicadoresInstitucion(institucion_id=1, periodo=PERIODO, **campos)  # type: ignore[arg-type]


#: Institución que cumple los tres criterios de "no recomendable".
def _en_riesgo() -> IndicadoresInstitucion:
    return _indicadores(
        imor=Decimal("9.0"),
        icap=Decimal("11.0"),
        crecimiento_captacion_pct=Decimal("80.0"),
    )


# ─── No recomendable ─────────────────────────────────────────


def test_the_three_conditions_together_are_red(umbrales: UmbralesBanderas) -> None:
    bandera = evaluar_no_recomendable(_en_riesgo(), umbrales)
    assert bandera is not None
    assert bandera.severidad is Severidad.ROJA
    assert bandera.compuesta is True
    assert "capta para cubrir deudas previas" in bandera.motivo


@pytest.mark.parametrize(
    ("campo", "valor_sano"),
    [
        ("imor", Decimal("2.0")),
        ("icap", Decimal("20.0")),
        ("crecimiento_captacion_pct", Decimal("5.0")),
    ],
)
def test_any_single_condition_missing_defuses_it(
    campo: str, valor_sano: Decimal, umbrales: UmbralesBanderas
) -> None:
    """La compuesta necesita las tres: dos de tres no bastan."""
    indicadores = _en_riesgo().model_copy(update={campo: valor_sano})
    assert evaluar_no_recomendable(indicadores, umbrales) is None


def test_missing_data_defuses_it(umbrales: UmbralesBanderas) -> None:
    """Sin crecimiento de captación no se puede afirmar el patrón."""
    indicadores = _indicadores(imor=Decimal("9.0"), icap=Decimal("11.0"))
    assert evaluar_no_recomendable(indicadores, umbrales) is None


# ─── Red flag de tasa ────────────────────────────────────────


def test_high_rate_with_troubled_collections_is_red(umbrales: UmbralesBanderas) -> None:
    bandera = evaluar_red_flag_tasa(
        _indicadores(imor=Decimal("5.0")),
        umbrales,
        tasa_ofrecida=Decimal("15.0"),
        mediana_mercado=Decimal("7.0"),
    )
    assert bandera is not None
    assert bandera.severidad is Severidad.ROJA
    assert "necesidad de liquidez" in bandera.motivo


def test_a_high_rate_alone_is_not_suspicious(umbrales: UmbralesBanderas) -> None:
    """Puede ser una institución eficiente compitiendo, y suele serlo."""
    assert (
        evaluar_red_flag_tasa(
            _indicadores(imor=Decimal("1.0")),
            umbrales,
            tasa_ofrecida=Decimal("15.0"),
            mediana_mercado=Decimal("7.0"),
        )
        is None
    )


def test_troubled_collections_alone_do_not_trigger_this_flag(
    umbrales: UmbralesBanderas,
) -> None:
    """Ese caso ya lo cubre la bandera individual de IMOR."""
    assert (
        evaluar_red_flag_tasa(
            _indicadores(imor=Decimal("5.0")),
            umbrales,
            tasa_ofrecida=Decimal("7.5"),
            mediana_mercado=Decimal("7.0"),
        )
        is None
    )


def test_market_context_is_required(umbrales: UmbralesBanderas) -> None:
    """Sin mediana de mercado no hay con qué comparar."""
    assert (
        evaluar_red_flag_tasa(
            _indicadores(imor=Decimal("5.0")),
            umbrales,
            tasa_ofrecida=Decimal("15.0"),
            mediana_mercado=None,
        )
        is None
    )


# ─── GAT inconsistente ───────────────────────────────────────


def test_inconsistent_gat_is_yellow_not_red(umbrales: UmbralesBanderas) -> None:
    """Es una señal para mirar el detalle, no un veredicto."""
    bandera = evaluar_gat_inconsistente(
        _indicadores(),
        umbrales,
        gat_publicada=Decimal("5.0"),
        tasa_nominal=Decimal("8.0"),
    )
    assert bandera is not None
    assert bandera.severidad is Severidad.AMARILLA
    assert bandera.tipo is TipoBandera.GAT_INCONSISTENTE


def test_consistent_gat_raises_nothing(umbrales: UmbralesBanderas) -> None:
    assert (
        evaluar_gat_inconsistente(
            _indicadores(),
            umbrales,
            gat_publicada=Decimal("7.85"),
            tasa_nominal=Decimal("8.0"),
        )
        is None
    )


# ─── Prioridad (nota de diseño de §5.2) ──────────────────────


def test_a_red_composite_suppresses_the_individual_flags(
    umbrales: UmbralesBanderas,
) -> None:
    """La regla central: nunca compuesta e individual a la vez.

    Mostrar la compuesta junto a las individuales que la componen sería repetir
    el mismo hallazgo y dar impresión de tres problemas donde hay uno.
    """
    banderas = evaluar_banderas(_en_riesgo(), umbrales)

    assert [b.tipo for b in banderas] == [TipoBandera.NO_RECOMENDABLE]
    assert all(b.compuesta for b in banderas)


def test_without_a_composite_the_most_severe_individuals_survive(
    umbrales: UmbralesBanderas,
) -> None:
    """Roja individual gana a amarilla individual."""
    banderas = evaluar_banderas(_indicadores(imor=Decimal("9.0"), icap=Decimal("12.0")), umbrales)

    assert {b.tipo for b in banderas} == {TipoBandera.IMOR}
    assert all(b.severidad is Severidad.ROJA for b in banderas)


def test_several_flags_of_the_same_severity_all_survive(
    umbrales: UmbralesBanderas,
) -> None:
    banderas = evaluar_banderas(_indicadores(imor=Decimal("9.0"), icap=Decimal("8.0")), umbrales)
    assert {b.tipo for b in banderas} == {TipoBandera.IMOR, TipoBandera.ICAP}


def test_a_healthy_institution_carries_no_flags(umbrales: UmbralesBanderas) -> None:
    banderas = evaluar_banderas(
        _indicadores(imor=Decimal("1.5"), icap=Decimal("22.0"), icor=Decimal("150.0")),
        umbrales,
    )
    assert banderas == []


def test_missing_coverage_survives_a_red_composite(umbrales: UmbralesBanderas) -> None:
    """Excepción deliberada: es un hecho estructural, no un hallazgo de salud.

    El usuario necesita saber que no hay seguro de depósitos aunque haya algo
    más grave que mirar.
    """
    banderas = evaluar_banderas(_en_riesgo(), umbrales, tipo_seguro=TipoSeguro.NINGUNO)

    tipos = {b.tipo for b in banderas}
    assert TipoBandera.NO_RECOMENDABLE in tipos
    assert TipoBandera.SIN_COBERTURA in tipos


def test_covered_institutions_get_no_coverage_flag(umbrales: UmbralesBanderas) -> None:
    banderas = evaluar_banderas(_en_riesgo(), umbrales, tipo_seguro=TipoSeguro.IPAB)
    assert TipoBandera.SIN_COBERTURA not in {b.tipo for b in banderas}


def test_priority_resolution_of_an_empty_list() -> None:
    assert resolver_prioridad([]) == []


# ─── Invariante global ───────────────────────────────────────


@pytest.mark.parametrize("imor", ["1.0", "4.0", "9.0", "20.0"])
@pytest.mark.parametrize("icap", ["8.0", "11.0", "22.0"])
@pytest.mark.parametrize("crecimiento", ["5.0", "80.0"])
def test_a_composite_and_an_individual_never_coexist(
    imor: str, icap: str, crecimiento: str, umbrales: UmbralesBanderas
) -> None:
    """Property test de la nota de diseño de §5.2, sobre 24 combinaciones."""
    banderas = evaluar_banderas(
        _indicadores(
            imor=Decimal(imor),
            icap=Decimal(icap),
            crecimiento_captacion_pct=Decimal(crecimiento),
        ),
        umbrales,
    )

    hay_compuesta_roja = any(b.compuesta and b.severidad is Severidad.ROJA for b in banderas)
    hay_individual = any(not b.compuesta for b in banderas)
    assert not (hay_compuesta_roja and hay_individual)


@pytest.mark.parametrize("imor", ["1.0", "4.0", "9.0"])
@pytest.mark.parametrize("icap", ["8.0", "12.0", "22.0"])
def test_severity_levels_are_never_mixed(imor: str, icap: str, umbrales: UmbralesBanderas) -> None:
    """Si hay una roja, no se muestran amarillas junto a ella."""
    banderas = [
        b
        for b in evaluar_banderas(_indicadores(imor=Decimal(imor), icap=Decimal(icap)), umbrales)
        if b.tipo is not TipoBandera.SIN_COBERTURA
    ]
    severidades = {b.severidad for b in banderas}
    assert len(severidades) <= 1
