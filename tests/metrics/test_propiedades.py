"""Property tests del motor de métricas.

Los tests por ejemplo comprueban casos que se nos ocurrieron. Éstos comprueban
propiedades que deben cumplirse siempre, sobre entradas que hypothesis elige —
incluidas las que no se nos habrían ocurrido.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from domain.enums import Severidad, TipoInstrumento, TipoSeguro
from domain.models import IndicadoresInstitucion, ParametrosFiscales, UmbralesBanderas
from metrics.coverage import cobertura_mxn, resolver_cobertura
from metrics.flags import evaluar_banderas
from metrics.real import desglose_cascada
from metrics.ten import ten

# ─── Estrategias ──────────────────────────────────────────────

montos = st.decimals(
    min_value=Decimal("1"), max_value=Decimal("100000000"), places=2, allow_nan=False
)
tasas = st.decimals(min_value=Decimal("0"), max_value=Decimal("100"), places=2, allow_nan=False)
inflaciones = st.decimals(
    min_value=Decimal("-5"), max_value=Decimal("50"), places=2, allow_nan=False
)
plazos = st.integers(min_value=1, max_value=3650)
instrumentos = st.sampled_from(list(TipoInstrumento))
porcentajes = st.decimals(
    min_value=Decimal("0"), max_value=Decimal("100"), places=2, allow_nan=False
)


def _params(retencion: Decimal = Decimal("0.90")) -> ParametrosFiscales:
    return ParametrosFiscales(
        anio=2026, tasa_retencion_capital=retencion, vigente_desde=date(2026, 1, 1)
    )


# ─── Cascada ──────────────────────────────────────────────────


@given(monto=montos, tasa=tasas, plazo=plazos, inflacion=inflaciones, instrumento=instrumentos)
@settings(max_examples=300, deadline=None)
def test_the_cascade_always_adds_up(
    monto: Decimal,
    tasa: Decimal,
    plazo: int,
    inflacion: Decimal,
    instrumento: TipoInstrumento,
) -> None:
    """Invariante central: bruto = ISR + inflación + real, exacto."""
    resultado = desglose_cascada(monto, tasa, instrumento, plazo, inflacion, _params())

    assert (
        resultado.rendimiento_bruto
        == resultado.isr_retenido + resultado.efecto_inflacion + resultado.ganancia_real
    )


@given(monto=montos, tasa=tasas, plazo=plazos, inflacion=inflaciones)
@settings(max_examples=200, deadline=None)
def test_net_yield_never_exceeds_gross(
    monto: Decimal, tasa: Decimal, plazo: int, inflacion: Decimal
) -> None:
    resultado = desglose_cascada(monto, tasa, TipoInstrumento.CETES, plazo, inflacion, _params())
    assert resultado.rendimiento_neto <= resultado.rendimiento_bruto


@given(monto=montos, tasa=tasas, plazo=plazos, baja=inflaciones, alta=inflaciones)
@settings(max_examples=200, deadline=None)
def test_more_inflation_never_increases_the_real_gain(
    monto: Decimal, tasa: Decimal, plazo: int, baja: Decimal, alta: Decimal
) -> None:
    """Monotonía en la inflación."""
    assume(baja < alta)

    con_baja = desglose_cascada(
        monto, tasa, TipoInstrumento.CETES, plazo, baja, _params()
    ).ganancia_real
    con_alta = desglose_cascada(
        monto, tasa, TipoInstrumento.CETES, plazo, alta, _params()
    ).ganancia_real

    assert con_alta <= con_baja


@given(monto=montos, menor=tasas, mayor=tasas, plazo=plazos, inflacion=inflaciones)
@settings(max_examples=200, deadline=None)
def test_more_nominal_never_decreases_the_real_gain(
    monto: Decimal, menor: Decimal, mayor: Decimal, plazo: int, inflacion: Decimal
) -> None:
    """Monotonía en la tasa nominal, ceteris paribus."""
    assume(menor < mayor)

    con_menor = desglose_cascada(
        monto, menor, TipoInstrumento.CETES, plazo, inflacion, _params()
    ).ganancia_real
    con_mayor = desglose_cascada(
        monto, mayor, TipoInstrumento.CETES, plazo, inflacion, _params()
    ).ganancia_real

    assert con_mayor >= con_menor


@given(tasa=tasas, retencion=porcentajes, instrumento=instrumentos)
@settings(max_examples=200, deadline=None)
def test_more_nominal_never_decreases_the_ten(
    tasa: Decimal, retencion: Decimal, instrumento: TipoInstrumento
) -> None:
    """Monotonía de la TEN."""
    params = _params(retencion)
    assert ten(tasa, instrumento, params) <= ten(tasa + 1, instrumento, params)


@given(tasa=tasas, retencion=porcentajes, instrumento=instrumentos)
@settings(max_examples=200, deadline=None)
def test_ten_never_exceeds_the_nominal_rate(
    tasa: Decimal, retencion: Decimal, instrumento: TipoInstrumento
) -> None:
    assert ten(tasa, instrumento, _params(retencion)) <= tasa


@given(monto=montos, tasa=tasas, plazo=plazos, inflacion=inflaciones)
@settings(max_examples=200, deadline=None)
def test_a_higher_withholding_never_increases_the_net_yield(
    monto: Decimal, tasa: Decimal, plazo: int, inflacion: Decimal
) -> None:
    """Subir la retención sólo puede perjudicar al ahorrador."""
    con_2025 = desglose_cascada(
        monto, tasa, TipoInstrumento.CETES, plazo, inflacion, _params(Decimal("0.50"))
    ).rendimiento_neto
    con_2026 = desglose_cascada(
        monto, tasa, TipoInstrumento.CETES, plazo, inflacion, _params(Decimal("0.90"))
    ).rendimiento_neto

    assert con_2026 <= con_2025


# ─── Cobertura ────────────────────────────────────────────────


@given(
    udi=st.decimals(
        min_value=Decimal("0.000001"), max_value=Decimal("1000"), places=6, allow_nan=False
    ),
    seguro=st.sampled_from(list(TipoSeguro)),
)
@settings(max_examples=200, deadline=None)
def test_coverage_is_never_negative(udi: Decimal, seguro: TipoSeguro) -> None:
    limite = cobertura_mxn(seguro, udi)
    assert limite is None or limite >= 0


@given(
    udi=st.decimals(min_value=Decimal("1"), max_value=Decimal("100"), places=6, allow_nan=False),
    monto=montos,
    seguro=st.sampled_from(list(TipoSeguro)),
)
@settings(max_examples=200, deadline=None)
def test_exposure_and_coverage_are_consistent(
    udi: Decimal, monto: Decimal, seguro: TipoSeguro
) -> None:
    """Si está cubierto no hay exposición, y si hay exposición no está cubierto."""
    cobertura = resolver_cobertura(seguro, udi)
    expuesto = cobertura.monto_expuesto(monto)

    assert expuesto >= 0
    assert expuesto <= monto
    assert cobertura.cubre(monto) is (expuesto == 0)


# ─── Banderas ─────────────────────────────────────────────────

indicadores_arbitrarios = st.builds(
    IndicadoresInstitucion,
    institucion_id=st.just(1),
    periodo=st.just(date(2026, 3, 31)),
    imor=st.none() | porcentajes,
    icap=st.none() | porcentajes,
    icor=st.none() | st.decimals(min_value=0, max_value=500, places=2, allow_nan=False),
    crecimiento_captacion_pct=st.none()
    | st.decimals(min_value=-100, max_value=500, places=2, allow_nan=False),
    pasivo_total=st.none() | montos,
    capital_contable=st.none() | montos,
)


@given(indicadores=indicadores_arbitrarios)
@settings(max_examples=300, deadline=None)
def test_a_composite_and_an_individual_never_coexist(
    indicadores: IndicadoresInstitucion,
) -> None:
    """Nota de diseño de §5.2, sobre entradas arbitrarias."""
    banderas = evaluar_banderas(indicadores, UmbralesBanderas())

    hay_compuesta_roja = any(b.compuesta and b.severidad is Severidad.ROJA for b in banderas)
    hay_individual = any(not b.compuesta for b in banderas)

    assert not (hay_compuesta_roja and hay_individual)


@given(indicadores=indicadores_arbitrarios)
@settings(max_examples=300, deadline=None)
def test_severity_levels_are_never_mixed(indicadores: IndicadoresInstitucion) -> None:
    banderas = evaluar_banderas(indicadores, UmbralesBanderas())
    assert len({b.severidad for b in banderas}) <= 1


@given(indicadores=indicadores_arbitrarios)
@settings(max_examples=200, deadline=None)
def test_every_flag_carries_a_reason_and_a_period(
    indicadores: IndicadoresInstitucion,
) -> None:
    """§10 y §11: ninguna bandera puede aparecer sin explicación ni fecha."""
    for bandera in evaluar_banderas(indicadores, UmbralesBanderas()):
        assert bandera.motivo.strip()
        assert bandera.periodo_dato == indicadores.periodo


@given(periodo=st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31)))
@settings(max_examples=100, deadline=None)
def test_an_institution_without_data_never_gets_flagged(periodo: date) -> None:
    """Sin dato se calla, sea cual sea el periodo."""
    vacios = IndicadoresInstitucion(institucion_id=1, periodo=periodo)
    assert evaluar_banderas(vacios, UmbralesBanderas()) == []
