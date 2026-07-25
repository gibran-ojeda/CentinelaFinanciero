"""Estado por indicador para la ficha de detalle.

La propiedad que importa no es el mapeo en sí sino que **no pueda divergir de
las banderas**: si la tarjeta usara sus propios umbrales, un día diría "en
rango" con la bandera roja al lado. Por eso hay un test que compara ambas
salidas sobre el mismo indicador, celda por celda.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from domain.enums import EstadoIndicador, NivelCapitalizacion, Severidad, UnidadIndicador
from domain.models import IndicadoresInstitucion, UmbralesBanderas
from metrics.flags import evaluar_indicadores, evaluar_individuales

PERIODO = date(2026, 3, 31)


@pytest.fixture
def umbrales() -> UmbralesBanderas:
    return UmbralesBanderas()


def _indicadores(**campos: object) -> IndicadoresInstitucion:
    return IndicadoresInstitucion(institucion_id=1, periodo=PERIODO, **campos)  # type: ignore[arg-type]


def _estado(clave: str, indicadores: IndicadoresInstitucion, umbrales: UmbralesBanderas) -> str:
    evaluado = next(i for i in evaluar_indicadores(indicadores, umbrales) if i.clave == clave)
    return evaluado.estado


# ─── Una celda por umbral ─────────────────────────────────────


@pytest.mark.parametrize(
    ("imor", "esperado"),
    [
        (Decimal("1.0"), EstadoIndicador.EN_RANGO),
        (Decimal("2.99"), EstadoIndicador.EN_RANGO),
        (Decimal("3.0"), EstadoIndicador.ATENCION),
        (Decimal("6.0"), EstadoIndicador.ATENCION),
        (Decimal("6.01"), EstadoIndicador.ALERTA),
        (None, EstadoIndicador.SIN_DATO),
    ],
)
def test_imor_status(imor: Decimal | None, esperado: str, umbrales: UmbralesBanderas) -> None:
    assert _estado("IMOR", _indicadores(imor=imor), umbrales) is esperado


@pytest.mark.parametrize(
    ("icap", "esperado"),
    [
        (Decimal("20.0"), EstadoIndicador.EN_RANGO),
        (Decimal("14.9"), EstadoIndicador.ATENCION),
        (Decimal("10.4"), EstadoIndicador.ALERTA),
        (None, EstadoIndicador.SIN_DATO),
    ],
)
def test_icap_status(icap: Decimal | None, esperado: str, umbrales: UmbralesBanderas) -> None:
    assert _estado("ICAP", _indicadores(icap=icap), umbrales) is esperado


@pytest.mark.parametrize(
    ("icor", "esperado"),
    [
        (Decimal("120.0"), EstadoIndicador.EN_RANGO),
        (Decimal("99.0"), EstadoIndicador.ATENCION),
        (Decimal("69.0"), EstadoIndicador.ALERTA),
        (None, EstadoIndicador.SIN_DATO),
    ],
)
def test_icor_status(icor: Decimal | None, esperado: str, umbrales: UmbralesBanderas) -> None:
    assert _estado("ICOR", _indicadores(icor=icor), umbrales) is esperado


@pytest.mark.parametrize(
    ("nivel", "esperado"),
    [
        (NivelCapitalizacion.N1, EstadoIndicador.EN_RANGO),
        (NivelCapitalizacion.N2, EstadoIndicador.ATENCION),
        (NivelCapitalizacion.N3, EstadoIndicador.ALERTA),
        (NivelCapitalizacion.N4, EstadoIndicador.ALERTA),
        (None, EstadoIndicador.SIN_DATO),
    ],
)
def test_nicap_status(
    nivel: NivelCapitalizacion | None, esperado: str, umbrales: UmbralesBanderas
) -> None:
    assert _estado("NICAP", _indicadores(nicap_nivel=nivel), umbrales) is esperado


# ─── Coherencia con las banderas ──────────────────────────────


@pytest.mark.parametrize(
    "indicadores",
    [
        _indicadores(imor=Decimal("1.0"), icap=Decimal("20.0"), icor=Decimal("130.0")),
        _indicadores(imor=Decimal("4.0"), icap=Decimal("14.0"), icor=Decimal("95.0")),
        _indicadores(imor=Decimal("9.0"), icap=Decimal("9.0"), icor=Decimal("60.0")),
        _indicadores(nicap_nivel=NivelCapitalizacion.N2),
        _indicadores(),
    ],
)
def test_a_card_can_never_contradict_its_flag(
    indicadores: IndicadoresInstitucion, umbrales: UmbralesBanderas
) -> None:
    """El test que hace que duplicar umbrales deje de ser posible en silencio."""
    severidad_por_tipo = {
        b.tipo.value: b.severidad for b in evaluar_individuales(indicadores, umbrales)
    }
    equivalencia = {
        Severidad.AMARILLA: EstadoIndicador.ATENCION,
        Severidad.ROJA: EstadoIndicador.ALERTA,
    }
    # El motor nombra la regla del ICOR COBERTURA_CARTERA; la tarjeta usa el
    # nombre del indicador. Es el único punto donde ambos difieren.
    por_bandera = {"ICOR": "COBERTURA_CARTERA"}

    for evaluado in evaluar_indicadores(indicadores, umbrales):
        if evaluado.estado in (EstadoIndicador.SIN_DATO, EstadoIndicador.INFORMATIVO):
            continue
        tipo = por_bandera.get(evaluado.clave, evaluado.clave)
        severidad = severidad_por_tipo.get(tipo)
        if severidad is None:
            assert evaluado.estado is EstadoIndicador.EN_RANGO, evaluado.clave
        else:
            assert evaluado.estado is equivalencia[severidad], evaluado.clave


# ─── Casos con criterio propio ────────────────────────────────


def test_deposits_have_a_value_but_no_threshold(umbrales: UmbralesBanderas) -> None:
    """Un saldo grande no es bueno ni malo: marcarlo "en rango" sería inventar.

    La captación entra en la compuesta de §5.2 como *crecimiento*, nunca como
    nivel, así que la tarjeta la presenta como contexto y no como señal.
    """
    evaluado = next(
        i
        for i in evaluar_indicadores(_indicadores(captacion=Decimal("98300000000")), umbrales)
        if i.clave == "CAPTACION"
    )

    assert evaluado.estado is EstadoIndicador.INFORMATIVO
    assert evaluado.unidad is UnidadIndicador.MONEDA
    assert evaluado.valor == Decimal("98300000000")


def test_nicap_travels_as_text_not_as_a_number(umbrales: UmbralesBanderas) -> None:
    """ "N2" es una categoría de la CNBV, no una cantidad."""
    evaluado = next(
        i
        for i in evaluar_indicadores(_indicadores(nicap_nivel=NivelCapitalizacion.N2), umbrales)
        if i.clave == "NICAP"
    )

    assert evaluado.unidad is UnidadIndicador.NIVEL
    assert evaluado.valor is None
    assert evaluado.valor_texto == "N2"


def test_every_indicator_is_returned_even_without_data(umbrales: UmbralesBanderas) -> None:
    """Un hueco explícito dice más que una tarjeta ausente.

    Mientras la ingesta de la CNBV no exista (fase 8), casi todas las
    instituciones no tienen ningún indicador: la ficha debe poder decir "sin
    dato" en vez de quedarse en blanco sin explicación.
    """
    evaluados = evaluar_indicadores(_indicadores(), umbrales)

    assert {i.clave for i in evaluados} == {
        "IMOR",
        "ICAP",
        "NICAP",
        "ICOR",
        "APALANCAMIENTO",
        "CAPTACION",
    }
    assert all(i.estado is EstadoIndicador.SIN_DATO for i in evaluados)


def test_thresholds_are_injected_not_imported(umbrales: UmbralesBanderas) -> None:
    """Mover el umbral en ConfigStore mueve el semáforo, sin desplegar."""
    indicadores = _indicadores(imor=Decimal("4.0"))
    assert _estado("IMOR", indicadores, umbrales) is EstadoIndicador.ATENCION

    laxos = UmbralesBanderas(imor_amarilla=Decimal("5.0"), imor_roja=Decimal("8.0"))
    assert _estado("IMOR", indicadores, laxos) is EstadoIndicador.EN_RANGO
