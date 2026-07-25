"""Combinación de instrumentos y optimizador.

Dos propiedades mandan sobre todo lo demás, porque son las que pueden hacer
daño si fallan:

1. El tope de cobertura se comparte por **institución**. Si dos productos del
   mismo emisor contaran cada uno con su propio tope, la herramienta diría
   "cubierto" sobre dinero que no lo está.
2. La cascada agregada cuadra al centavo. El usuario suma lo que ve.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from domain.enums import TipoInstrumento, TipoSeguro
from domain.models import ParametrosFiscales
from metrics.coverage import LIMITE_IPAB_UDIS, LIMITE_PROSOFIPO_UDIS
from metrics.portfolio import (
    Candidato,
    elegibles,
    evaluar_combinacion,
    normalizar,
    optimizar,
)

#: Redonda a propósito: hace que los topes salgan en números que se pueden
#: comprobar a mano (IPAB = $4,000,000; PROSOFIPO = $250,000).
UDI = Decimal("10")

CETES = Candidato(
    producto_id=1,
    institucion_id=100,
    tipo_seguro=TipoSeguro.SOBERANO,
    instrumento=TipoInstrumento.CETES,
    tasa_nominal=Decimal("6.93"),
    plazo_dias=364,
    monto_minimo=Decimal("100"),
)
BANCO = Candidato(
    producto_id=2,
    institucion_id=200,
    tipo_seguro=TipoSeguro.IPAB,
    instrumento=TipoInstrumento.DEPOSITO_BANCARIO,
    tasa_nominal=Decimal("12.00"),
    plazo_dias=None,
    monto_minimo=Decimal("0"),
)
SOFIPO_A = Candidato(
    producto_id=3,
    institucion_id=300,
    tipo_seguro=TipoSeguro.PROSOFIPO,
    instrumento=TipoInstrumento.DEPOSITO_SOFIPO,
    tasa_nominal=Decimal("13.40"),
    plazo_dias=91,
    monto_minimo=Decimal("100"),
)
#: Mismo emisor que SOFIPO_A: el caso que prueba el tope compartido.
SOFIPO_A_BIS = Candidato(
    producto_id=4,
    institucion_id=300,
    tipo_seguro=TipoSeguro.PROSOFIPO,
    instrumento=TipoInstrumento.DEPOSITO_SOFIPO,
    tasa_nominal=Decimal("11.00"),
    plazo_dias=None,
    monto_minimo=Decimal("0"),
)
IFPE = Candidato(
    producto_id=5,
    institucion_id=500,
    tipo_seguro=TipoSeguro.NINGUNO,
    instrumento=TipoInstrumento.DEPOSITO_BANCARIO,
    tasa_nominal=Decimal("15.00"),
    plazo_dias=None,
    monto_minimo=Decimal("0"),
)
RIESGOSA = Candidato(
    producto_id=6,
    institucion_id=600,
    tipo_seguro=TipoSeguro.PROSOFIPO,
    instrumento=TipoInstrumento.DEPOSITO_SOFIPO,
    tasa_nominal=Decimal("16.50"),
    plazo_dias=182,
    monto_minimo=Decimal("10000"),
    tiene_bandera_roja=True,
)


#: Horizonte por defecto de los tests. Es uno de los que ofrece la UI y —lo
#: que importa aquí— da cabida a CETES a 364 días: con un horizonte más corto
#: el optimizador lo descarta por iliquidez, que es correcto pero deja fuera
#: al único instrumento sin tope de cobertura.
HORIZONTE = 364


def _evaluar(candidatos, porcentajes, fiscal_2026, **kwargs):  # type: ignore[no-untyped-def]
    opciones = {
        "monto_total": Decimal("100000"),
        "horizonte_dias": HORIZONTE,
        "inflacion_anual": Decimal("4.5"),
        "params": fiscal_2026,
        "valor_udi": UDI,
    }
    opciones.update(kwargs)
    return evaluar_combinacion(candidatos, porcentajes, **opciones)  # type: ignore[arg-type]


# ─── Normalización ────────────────────────────────────────────


@pytest.mark.parametrize(
    "pesos",
    [
        [Decimal("50"), Decimal("50")],
        [Decimal("70"), Decimal("40")],
        [Decimal("1"), Decimal("1"), Decimal("1")],
        [Decimal("99.9"), Decimal("0.1")],
        [Decimal("0"), Decimal("0")],
        [Decimal("33.3"), Decimal("33.3"), Decimal("33.3")],
    ],
)
def test_normalisation_always_sums_to_exactly_one_hundred(pesos: list[Decimal]) -> None:
    assert sum(normalizar(pesos)) == Decimal("100")


def test_normalisation_preserves_proportions() -> None:
    """70 y 40 es el mismo reparto que 63.6 y 36.4."""
    assert normalizar([Decimal("70"), Decimal("40")]) == [Decimal("63.6"), Decimal("36.4")]


def test_zero_weights_split_evenly() -> None:
    assert normalizar([Decimal("0")] * 4) == [Decimal("25.0")] * 4


def test_an_empty_portfolio_normalises_to_nothing() -> None:
    assert normalizar([]) == []


# ─── Cobertura compartida por institución ─────────────────────


def test_two_products_of_one_institution_share_the_coverage_cap(
    fiscal_2026: ParametrosFiscales,
) -> None:
    """La propiedad que evita decirle al usuario que está cubierto sin estarlo.

    PROSOFIPO cubre 25,000 UDIs. Con la UDI a $10 son $250,000 por persona y
    por institución — no por producto. Repartir $400,000 entre dos productos
    de la misma SOFIPO deja $150,000 fuera, no cero.
    """
    combinacion = _evaluar(
        [SOFIPO_A, SOFIPO_A_BIS],
        [Decimal("50"), Decimal("50")],
        fiscal_2026,
        monto_total=Decimal("400000"),
    )

    tope = LIMITE_PROSOFIPO_UDIS * UDI
    assert tope == Decimal("250000")
    assert combinacion.monto_protegido == tope
    assert combinacion.porcentaje_protegido == Decimal("62")

    primera, segunda = combinacion.asignaciones
    assert primera.monto_cubierto == Decimal("200000.00")  # cabe entero
    assert segunda.monto_cubierto == Decimal("50000.00")  # sólo lo que quedaba
    assert segunda.monto_expuesto == Decimal("150000.00")


def test_two_institutions_each_get_their_own_cap(fiscal_2026: ParametrosFiscales) -> None:
    """Repartir entre emisores distintos sí multiplica la protección."""
    combinacion = _evaluar(
        [SOFIPO_A, RIESGOSA],
        [Decimal("50"), Decimal("50")],
        fiscal_2026,
        monto_total=Decimal("400000"),
    )

    assert all(a.cubierto for a in combinacion.asignaciones)
    assert combinacion.porcentaje_protegido == Decimal("100")


def test_sovereign_debt_has_no_cap(fiscal_2026: ParametrosFiscales) -> None:
    combinacion = _evaluar([CETES], [Decimal("100")], fiscal_2026, monto_total=Decimal("50000000"))

    assert combinacion.asignaciones[0].cubierto
    assert combinacion.porcentaje_protegido == Decimal("100")


def test_an_ifpe_protects_nothing(fiscal_2026: ParametrosFiscales) -> None:
    combinacion = _evaluar([IFPE], [Decimal("100")], fiscal_2026)

    assert combinacion.monto_protegido == Decimal("0.00")
    assert combinacion.asignaciones[0].monto_expuesto == Decimal("100000.00")


def test_the_protected_percentage_is_floored_never_rounded_up(
    fiscal_2026: ParametrosFiscales,
) -> None:
    """Un peso sin cubrir hace que la respuesta sea 99%, no 100%."""
    tope = LIMITE_IPAB_UDIS * UDI
    combinacion = _evaluar([BANCO], [Decimal("100")], fiscal_2026, monto_total=tope + Decimal("1"))

    assert combinacion.asignaciones[0].monto_expuesto == Decimal("1.00")
    assert combinacion.porcentaje_protegido == Decimal("99")


# ─── Cascada agregada ─────────────────────────────────────────


def test_the_aggregate_cascade_adds_up_to_the_cent(fiscal_2026: ParametrosFiscales) -> None:
    """El usuario suma lo que ve: bruto = ISR + inflación + real."""
    combinacion = _evaluar(
        [CETES, BANCO, SOFIPO_A],
        [Decimal("40"), Decimal("35"), Decimal("25")],
        fiscal_2026,
    )

    assert (
        combinacion.rendimiento_bruto
        == combinacion.isr_retenido + combinacion.efecto_inflacion + combinacion.ganancia_real
    )
    assert combinacion.rendimiento_neto == combinacion.rendimiento_bruto - combinacion.isr_retenido


def test_the_amounts_add_up_to_the_total(fiscal_2026: ParametrosFiscales) -> None:
    """Sin esto, el detalle por instrumento no cuadraría con el encabezado."""
    combinacion = _evaluar(
        [CETES, BANCO, SOFIPO_A],
        [Decimal("33.3"), Decimal("33.3"), Decimal("33.4")],
        fiscal_2026,
    )

    assert sum(a.monto for a in combinacion.asignaciones) == combinacion.monto_total


def test_the_weighted_ten_is_weighted_by_amount_not_by_count(
    fiscal_2026: ParametrosFiscales,
) -> None:
    """90% en el que rinde poco tiene que arrastrar la media hacia abajo."""
    combinacion = _evaluar([CETES, BANCO], [Decimal("90"), Decimal("10")], fiscal_2026)

    ten_cetes = Decimal("6.93") - Decimal("0.90")
    ten_banco = Decimal("12.00") - Decimal("0.90")
    esperada = ten_cetes * Decimal("0.9") + ten_banco * Decimal("0.1")

    assert combinacion.ten_ponderada == esperada.quantize(Decimal("0.0001"))


def test_an_empty_portfolio_is_all_zeros(fiscal_2026: ParametrosFiscales) -> None:
    combinacion = _evaluar([], [], fiscal_2026)

    assert combinacion.asignaciones == []
    assert combinacion.rendimiento_bruto == Decimal("0.00")
    assert combinacion.porcentaje_protegido == Decimal("0")


def test_an_instrument_at_zero_percent_stays_in_the_list(
    fiscal_2026: ParametrosFiscales,
) -> None:
    """Desaparecer de la respuesta borraría de la pantalla algo que se eligió."""
    combinacion = _evaluar([CETES, BANCO], [Decimal("100"), Decimal("0")], fiscal_2026)

    assert len(combinacion.asignaciones) == 2
    assert combinacion.asignaciones[1].monto == Decimal("0.00")
    assert combinacion.asignaciones[1].cascada.rendimiento_bruto == Decimal("0.00")


# ─── Advertencia de liquidez ──────────────────────────────────


def test_a_term_beyond_the_horizon_is_flagged(fiscal_2026: ParametrosFiscales) -> None:
    """El cálculo se hace, pero no en silencio."""
    combinacion = _evaluar([CETES], [Decimal("100")], fiscal_2026, horizonte_dias=91)

    aviso = combinacion.asignaciones[0].advertencia_liquidez
    assert aviso is not None
    assert "364" in aviso and "91" in aviso


def test_a_term_within_the_horizon_is_not_flagged(fiscal_2026: ParametrosFiscales) -> None:
    combinacion = _evaluar([SOFIPO_A], [Decimal("100")], fiscal_2026, horizonte_dias=91)
    assert combinacion.asignaciones[0].advertencia_liquidez is None


def test_sight_products_are_never_flagged(fiscal_2026: ParametrosFiscales) -> None:
    combinacion = _evaluar([BANCO], [Decimal("100")], fiscal_2026, horizonte_dias=28)
    assert combinacion.asignaciones[0].advertencia_liquidez is None


# ─── Validación ───────────────────────────────────────────────


@pytest.mark.parametrize("monto", [Decimal("0"), Decimal("-1")])
def test_a_non_positive_amount_is_rejected(
    monto: Decimal, fiscal_2026: ParametrosFiscales
) -> None:
    with pytest.raises(ValueError, match="monto total"):
        _evaluar([CETES], [Decimal("100")], fiscal_2026, monto_total=monto)


def test_mismatched_lengths_are_rejected(fiscal_2026: ParametrosFiscales) -> None:
    with pytest.raises(ValueError, match="porcentaje por candidato"):
        _evaluar([CETES, BANCO], [Decimal("100")], fiscal_2026)


def test_a_non_positive_horizon_is_rejected(fiscal_2026: ParametrosFiscales) -> None:
    with pytest.raises(ValueError, match="horizonte"):
        _evaluar([CETES], [Decimal("100")], fiscal_2026, horizonte_dias=0)


# ─── Elegibilidad ─────────────────────────────────────────────


def test_terms_beyond_the_horizon_are_not_eligible() -> None:
    """Proponer iliquidez sin decirlo sería peor que no proponer nada."""
    resultado = elegibles(
        [CETES, SOFIPO_A, BANCO],
        monto_total=Decimal("100000"),
        horizonte_dias=91,
        excluir_rojas=True,
    )
    assert {c.producto_id for c in resultado} == {SOFIPO_A.producto_id, BANCO.producto_id}


def test_products_above_the_users_capital_are_not_eligible() -> None:
    resultado = elegibles(
        [RIESGOSA, BANCO],
        monto_total=Decimal("5000"),
        horizonte_dias=360,
        excluir_rojas=False,
    )
    assert {c.producto_id for c in resultado} == {BANCO.producto_id}


def test_red_flagged_institutions_are_excluded_when_asked() -> None:
    comunes = {"monto_total": Decimal("100000"), "horizonte_dias": 360}
    con = elegibles([RIESGOSA, BANCO], **comunes, excluir_rojas=False)  # type: ignore[arg-type]
    sin = elegibles([RIESGOSA, BANCO], **comunes, excluir_rojas=True)  # type: ignore[arg-type]

    assert RIESGOSA in con
    assert RIESGOSA not in sin


# ─── Optimizador ──────────────────────────────────────────────


def _optimizar(candidatos, fiscal_2026, **kwargs):  # type: ignore[no-untyped-def]
    opciones = {
        "monto_total": Decimal("100000"),
        "horizonte_dias": HORIZONTE,
        "params": fiscal_2026,
        "valor_udi": UDI,
    }
    opciones.update(kwargs)
    return optimizar(candidatos, **opciones)  # type: ignore[arg-type]


def test_the_optimiser_fills_by_net_rate_first(fiscal_2026: ParametrosFiscales) -> None:
    """Ordena por TEN, no por tasa nominal: es la comparación honesta."""
    reparto = _optimizar([CETES, BANCO, SOFIPO_A], fiscal_2026)

    assert [c.producto_id for c, _ in reparto][0] == SOFIPO_A.producto_id
    assert sum(pct for _, pct in reparto) == Decimal("100")


def test_no_institution_exceeds_its_cap(fiscal_2026: ParametrosFiscales) -> None:
    """El criterio de la fase: $5M repartidos sin pasarse de ningún tope."""
    monto = Decimal("5000000")
    reparto = _optimizar([CETES, BANCO, SOFIPO_A], fiscal_2026, monto_total=monto)

    topes = {
        TipoSeguro.PROSOFIPO: LIMITE_PROSOFIPO_UDIS * UDI,
        TipoSeguro.IPAB: LIMITE_IPAB_UDIS * UDI,
    }
    for candidato, porcentaje in reparto:
        asignado = monto * porcentaje / Decimal("100")
        tope = topes.get(candidato.tipo_seguro)
        if tope is not None:
            assert asignado <= tope, candidato.producto_id


def test_the_remainder_goes_to_the_uncapped_instrument(
    fiscal_2026: ParametrosFiscales,
) -> None:
    """Con $5M, tras llenar SOFIPO e IPAB el resto sólo cabe en el soberano."""
    monto = Decimal("5000000")
    reparto = _optimizar([CETES, BANCO, SOFIPO_A], fiscal_2026, monto_total=monto)
    por_id = {c.producto_id: pct for c, pct in reparto}

    esperado_cetes = (
        (monto - LIMITE_PROSOFIPO_UDIS * UDI - LIMITE_IPAB_UDIS * UDI) * Decimal("100") / monto
    )
    assert por_id[CETES.producto_id] == esperado_cetes.quantize(Decimal("0.1"))


def test_one_product_per_institution(fiscal_2026: ParametrosFiscales) -> None:
    """El segundo del mismo emisor no protege más: sólo complica el reparto.

    Hace falta un monto que no se agote en el primero para que el caso ocurra:
    con $100,000 la SOFIPO se lo lleva todo y el bucle termina antes de llegar
    a su segundo producto.
    """
    reparto = _optimizar(
        [SOFIPO_A, SOFIPO_A_BIS, CETES], fiscal_2026, monto_total=Decimal("5000000")
    )

    instituciones = [c.institucion_id for c, _ in reparto]
    assert len(instituciones) == len(set(instituciones))
    assert SOFIPO_A_BIS.producto_id not in {c.producto_id for c, _ in reparto}
    assert {c.producto_id for c, _ in reparto} == {SOFIPO_A.producto_id, CETES.producto_id}


def test_the_optimiser_leaves_out_uncovered_issuers_when_protecting(
    fiscal_2026: ParametrosFiscales,
) -> None:
    """El IFPE rinde más que nadie, y aun así no entra.

    Pedir "respeta los límites de seguro" y recibir dinero en un emisor sin
    fondo de protección sería contradecir la instrucción.
    """
    reparto = _optimizar([IFPE, CETES], fiscal_2026, respetar_seguro=True)

    assert IFPE.producto_id not in {c.producto_id for c, _ in reparto}


def test_without_the_insurance_toggle_the_best_rate_takes_everything(
    fiscal_2026: ParametrosFiscales,
) -> None:
    reparto = _optimizar([IFPE, CETES], fiscal_2026, respetar_seguro=False)

    assert len(reparto) == 1
    assert reparto[0][0].producto_id == IFPE.producto_id
    assert reparto[0][1] == Decimal("100")


def test_the_optimiser_skips_red_flagged_institutions(
    fiscal_2026: ParametrosFiscales,
) -> None:
    reparto = _optimizar([RIESGOSA, CETES], fiscal_2026, excluir_rojas=True)
    assert RIESGOSA.producto_id not in {c.producto_id for c, _ in reparto}

    reparto = _optimizar([RIESGOSA, CETES], fiscal_2026, excluir_rojas=False)
    assert RIESGOSA.producto_id in {c.producto_id for c, _ in reparto}


def test_the_optimiser_is_deterministic(fiscal_2026: ParametrosFiscales) -> None:
    """Dos llamadas iguales proponen exactamente lo mismo."""
    uno = _optimizar([CETES, BANCO, SOFIPO_A], fiscal_2026)
    otro = _optimizar([CETES, BANCO, SOFIPO_A], fiscal_2026)

    assert [(c.producto_id, p) for c, p in uno] == [(c.producto_id, p) for c, p in otro]


def test_nothing_eligible_produces_an_empty_allocation(
    fiscal_2026: ParametrosFiscales,
) -> None:
    reparto = _optimizar([RIESGOSA], fiscal_2026, monto_total=Decimal("500"))
    assert reparto == []


def test_the_optimiser_rejects_a_non_positive_amount(
    fiscal_2026: ParametrosFiscales,
) -> None:
    with pytest.raises(ValueError, match="monto total"):
        _optimizar([CETES], fiscal_2026, monto_total=Decimal("0"))


def test_the_optimiser_output_feeds_the_evaluator(fiscal_2026: ParametrosFiscales) -> None:
    """Las dos mitades encajan: lo que propone se puede evaluar tal cual."""
    monto = Decimal("5000000")
    reparto = _optimizar([CETES, BANCO, SOFIPO_A], fiscal_2026, monto_total=monto)

    combinacion = _evaluar(
        [c for c, _ in reparto],
        [p for _, p in reparto],
        fiscal_2026,
        monto_total=monto,
    )

    assert combinacion.porcentaje_protegido == Decimal("100")
    assert (
        combinacion.rendimiento_bruto
        == combinacion.isr_retenido + combinacion.efecto_inflacion + combinacion.ganancia_real
    )
