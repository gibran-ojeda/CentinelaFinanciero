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

from domain.enums import RazonCorte, RazonDescarte, TipoInstrumento, TipoSeguro
from domain.models import ParametrosFiscales
from metrics.coverage import LIMITE_IPAB_UDIS, LIMITE_PROSOFIPO_UDIS
from metrics.portfolio import (
    Candidato,
    elegibles,
    evaluar_combinacion,
    evaluar_reparto,
    normalizar,
    optimizar,
)
from metrics.tramos import Tramo

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

    assert reparto.candidatos[0].producto_id == SOFIPO_A.producto_id
    assert sum(reparto.montos) == Decimal("100000")


def test_no_institution_exceeds_its_cap(fiscal_2026: ParametrosFiscales) -> None:
    """El criterio de la fase: $5M repartidos sin pasarse de ningún tope."""
    reparto = _optimizar([CETES, BANCO, SOFIPO_A], fiscal_2026, monto_total=Decimal("5000000"))

    topes = {
        TipoSeguro.PROSOFIPO: LIMITE_PROSOFIPO_UDIS * UDI,
        TipoSeguro.IPAB: LIMITE_IPAB_UDIS * UDI,
    }
    for candidato, monto in reparto.asignaciones:
        tope = topes.get(candidato.tipo_seguro)
        if tope is not None:
            assert monto <= tope, candidato.producto_id


def test_the_cap_survives_the_round_trip_through_percentages(
    fiscal_2026: ParametrosFiscales,
) -> None:
    """El defecto que motivó separar `evaluar_reparto` de `evaluar_combinacion`.

    El optimizador respeta cada tope al centavo, pero un porcentaje con un solo
    decimal sobre $5,000,000 tiene una granularidad de $5,000: pasar el reparto
    por porcentajes y de vuelta a importes colocaba a una institución por
    encima de su cobertura justo después de haberla respetado.
    """
    reparto = _optimizar([CETES, BANCO, SOFIPO_A], fiscal_2026, monto_total=Decimal("5000000"))

    combinacion = evaluar_reparto(
        reparto.candidatos,
        reparto.montos,
        horizonte_dias=HORIZONTE,
        inflacion_anual=Decimal("4.5"),
        params=fiscal_2026,
        valor_udi=UDI,
    )

    for asignacion in combinacion.asignaciones:
        limite = asignacion.cobertura.limite_mxn
        if limite is not None:
            assert asignacion.monto <= limite, asignacion.candidato.producto_id
    assert combinacion.porcentaje_protegido == Decimal("100")


def test_money_that_does_not_fit_is_declared_not_spread(
    fiscal_2026: ParametrosFiscales,
) -> None:
    """Sin emisor sin tope, la cobertura disponible se agota y sobra dinero.

    Repartir ese remanente entre los que ya están llenos anularía exactamente
    la garantía que el usuario pidió, así que se declara en vez de colocarse.
    """
    monto = Decimal("10000000")
    reparto = _optimizar([BANCO, SOFIPO_A], fiscal_2026, monto_total=monto)

    capacidad = LIMITE_IPAB_UDIS * UDI + LIMITE_PROSOFIPO_UDIS * UDI
    assert sum(reparto.montos) == capacidad
    assert reparto.monto_no_asignado == monto - capacidad


def test_with_an_uncapped_instrument_nothing_is_left_over(
    fiscal_2026: ParametrosFiscales,
) -> None:
    reparto = _optimizar([CETES, BANCO, SOFIPO_A], fiscal_2026, monto_total=Decimal("10000000"))

    assert reparto.monto_no_asignado == Decimal("0.00")
    assert sum(reparto.montos) == Decimal("10000000")


def test_the_remainder_goes_to_the_uncapped_instrument(
    fiscal_2026: ParametrosFiscales,
) -> None:
    """Con $5M, tras llenar SOFIPO e IPAB el resto sólo cabe en el soberano."""
    monto = Decimal("5000000")
    reparto = _optimizar([CETES, BANCO, SOFIPO_A], fiscal_2026, monto_total=monto)
    por_id = {c.producto_id: m for c, m in reparto.asignaciones}

    assert por_id[CETES.producto_id] == (
        monto - LIMITE_PROSOFIPO_UDIS * UDI - LIMITE_IPAB_UDIS * UDI
    )


def test_one_product_per_institution(fiscal_2026: ParametrosFiscales) -> None:
    """El segundo del mismo emisor no protege más: sólo complica el reparto.

    Hace falta un monto que no se agote en el primero para que el caso ocurra:
    con $100,000 la SOFIPO se lo lleva todo y el bucle termina antes de llegar
    a su segundo producto.
    """
    reparto = _optimizar(
        [SOFIPO_A, SOFIPO_A_BIS, CETES], fiscal_2026, monto_total=Decimal("5000000")
    )

    instituciones = [c.institucion_id for c in reparto.candidatos]
    assert len(instituciones) == len(set(instituciones))
    assert {c.producto_id for c in reparto.candidatos} == {
        SOFIPO_A.producto_id,
        CETES.producto_id,
    }


def test_the_optimiser_leaves_out_uncovered_issuers_when_protecting(
    fiscal_2026: ParametrosFiscales,
) -> None:
    """El IFPE rinde más que nadie, y aun así no entra.

    Pedir "respeta los límites de seguro" y recibir dinero en un emisor sin
    fondo de protección sería contradecir la instrucción.
    """
    reparto = _optimizar([IFPE, CETES], fiscal_2026, respetar_seguro=True)

    assert IFPE.producto_id not in {c.producto_id for c in reparto.candidatos}


def test_without_the_insurance_toggle_the_best_rate_takes_everything(
    fiscal_2026: ParametrosFiscales,
) -> None:
    reparto = _optimizar([IFPE, CETES], fiscal_2026, respetar_seguro=False)

    assert len(reparto.asignaciones) == 1
    assert reparto.candidatos[0].producto_id == IFPE.producto_id
    assert reparto.montos[0] == Decimal("100000")


def test_the_optimiser_skips_red_flagged_institutions(
    fiscal_2026: ParametrosFiscales,
) -> None:
    reparto = _optimizar([RIESGOSA, CETES], fiscal_2026, excluir_rojas=True)
    assert RIESGOSA.producto_id not in {c.producto_id for c in reparto.candidatos}

    reparto = _optimizar([RIESGOSA, CETES], fiscal_2026, excluir_rojas=False)
    assert RIESGOSA.producto_id in {c.producto_id for c in reparto.candidatos}


def test_the_optimiser_is_deterministic(fiscal_2026: ParametrosFiscales) -> None:
    """Dos llamadas iguales proponen exactamente lo mismo."""
    uno = _optimizar([CETES, BANCO, SOFIPO_A], fiscal_2026)
    otro = _optimizar([CETES, BANCO, SOFIPO_A], fiscal_2026)

    assert uno.asignaciones == otro.asignaciones


def test_nothing_eligible_produces_an_empty_allocation(
    fiscal_2026: ParametrosFiscales,
) -> None:
    reparto = _optimizar([RIESGOSA], fiscal_2026, monto_total=Decimal("500"))

    assert reparto.asignaciones == []
    assert reparto.monto_no_asignado == Decimal("500.00")


def test_mismatched_amounts_are_rejected(fiscal_2026: ParametrosFiscales) -> None:
    with pytest.raises(ValueError, match="monto por candidato"):
        evaluar_reparto(
            [CETES, BANCO],
            [Decimal("100")],
            horizonte_dias=HORIZONTE,
            inflacion_anual=Decimal("4.5"),
            params=fiscal_2026,
            valor_udi=UDI,
        )


def test_an_empty_allocation_evaluates_to_zeros(fiscal_2026: ParametrosFiscales) -> None:
    combinacion = evaluar_reparto(
        [],
        [],
        horizonte_dias=HORIZONTE,
        inflacion_anual=Decimal("4.5"),
        params=fiscal_2026,
        valor_udi=UDI,
    )

    assert combinacion.asignaciones == []
    assert combinacion.monto_total == Decimal("0")
    assert combinacion.porcentaje_protegido == Decimal("0")
    assert combinacion.ten_ponderada == Decimal("0.0000")


def test_the_optimiser_rejects_a_non_positive_amount(
    fiscal_2026: ParametrosFiscales,
) -> None:
    with pytest.raises(ValueError, match="monto total"):
        _optimizar([CETES], fiscal_2026, monto_total=Decimal("0"))


def test_the_optimiser_output_feeds_the_evaluator(fiscal_2026: ParametrosFiscales) -> None:
    """Las dos mitades encajan: lo que propone se puede evaluar tal cual."""
    reparto = _optimizar([CETES, BANCO, SOFIPO_A], fiscal_2026, monto_total=Decimal("5000000"))

    combinacion = evaluar_reparto(
        reparto.candidatos,
        reparto.montos,
        horizonte_dias=HORIZONTE,
        inflacion_anual=Decimal("4.5"),
        params=fiscal_2026,
        valor_udi=UDI,
    )

    assert combinacion.porcentaje_protegido == Decimal("100")
    assert (
        combinacion.rendimiento_bruto
        == combinacion.isr_retenido + combinacion.efecto_inflacion + combinacion.ganancia_real
    )
    assert sum(a.porcentaje for a in combinacion.asignaciones) == Decimal("100")


# ─── Optimizador con escaleras ────────────────────────────────

#: El caso Openbank: 13% los primeros $30,000, 6.3% de ahí a $1,000,000.
ESCALERA_OPENBANK = (
    Tramo(desde=Decimal("0"), hasta=Decimal("30000"), tasa_nominal=Decimal("13.00")),
    Tramo(desde=Decimal("30000"), hasta=Decimal("1000000"), tasa_nominal=Decimal("6.30")),
)

ESCALONADO = Candidato(
    producto_id=7,
    institucion_id=700,
    tipo_seguro=TipoSeguro.IPAB,
    instrumento=TipoInstrumento.DEPOSITO_BANCARIO,
    tasa_nominal=Decimal("13.00"),
    plazo_dias=None,
    monto_minimo=Decimal("0"),
    tramos=ESCALERA_OPENBANK,
)
PLANO_MEDIO = Candidato(
    producto_id=8,
    institucion_id=800,
    tipo_seguro=TipoSeguro.IPAB,
    instrumento=TipoInstrumento.DEPOSITO_BANCARIO,
    tasa_nominal=Decimal("9.00"),
    plazo_dias=None,
    monto_minimo=Decimal("0"),
)


def test_the_best_marginal_segment_crosses_products(fiscal_2026: ParametrosFiscales) -> None:
    """Lleno el tramo alto, el siguiente peso se va al otro emisor.

    El 13% de Openbank solo existe para $30,000; a partir de ahí su marginal
    (6.3%) pierde contra un 9% plano. El greedy clásico —una pasada por
    producto— habría dejado los $100,000 enteros en el escalonado.
    """
    reparto = _optimizar([ESCALONADO, PLANO_MEDIO], fiscal_2026)

    por_id = {c.producto_id: m for c, m in reparto.asignaciones}
    assert por_id[ESCALONADO.producto_id] == Decimal("30000.00")
    assert por_id[PLANO_MEDIO.producto_id] == Decimal("70000.00")
    # El escalonado abrió primero: su tramo alto era la mejor oferta inicial.
    assert reparto.candidatos[0].producto_id == ESCALONADO.producto_id


def test_a_minimum_above_the_first_ceiling_buys_the_lower_tier_too(
    fiscal_2026: ParametrosFiscales,
) -> None:
    """La oferta de entrada es la TEN efectiva del mínimo, no la del tramo 1.

    Con mínimo de $50,000 y tramo alto de $30,000, entrar cuesta comprar
    también $20,000 del tramo bajo: la entrada ofrece 10.32% (no 13%), aún
    mejor que el 10% plano de al lado — pero el marginal siguiente ya no.
    """
    escalonado_con_minimo = Candidato(
        producto_id=9,
        institucion_id=900,
        tipo_seguro=TipoSeguro.IPAB,
        instrumento=TipoInstrumento.DEPOSITO_BANCARIO,
        tasa_nominal=Decimal("13.00"),
        plazo_dias=None,
        monto_minimo=Decimal("50000"),
        tramos=ESCALERA_OPENBANK,
    )
    plano_diez = Candidato(
        producto_id=10,
        institucion_id=1000,
        tipo_seguro=TipoSeguro.IPAB,
        instrumento=TipoInstrumento.DEPOSITO_BANCARIO,
        tasa_nominal=Decimal("10.00"),
        plazo_dias=None,
        monto_minimo=Decimal("0"),
    )
    reparto = _optimizar(
        [escalonado_con_minimo, plano_diez], fiscal_2026, monto_total=Decimal("200000")
    )

    por_id = {c.producto_id: m for c, m in reparto.asignaciones}
    assert por_id[escalonado_con_minimo.producto_id] == Decimal("50000.00")
    assert por_id[plano_diez.producto_id] == Decimal("150000.00")


def test_a_product_that_cannot_reach_its_minimum_is_left_out(
    fiscal_2026: ParametrosFiscales,
) -> None:
    """Proponer una asignación por debajo del mínimo es proponer algo incontratable."""
    sofipo_llena = Candidato(
        producto_id=11,
        institucion_id=1100,
        tipo_seguro=TipoSeguro.PROSOFIPO,
        instrumento=TipoInstrumento.DEPOSITO_SOFIPO,
        tasa_nominal=Decimal("12.00"),
        plazo_dias=None,
        monto_minimo=Decimal("0"),
    )
    banco_exigente = Candidato(
        producto_id=12,
        institucion_id=1200,
        tipo_seguro=TipoSeguro.IPAB,
        instrumento=TipoInstrumento.DEPOSITO_BANCARIO,
        tasa_nominal=Decimal("11.00"),
        plazo_dias=None,
        monto_minimo=Decimal("80000"),
    )
    # La SOFIPO llena su tope de $250,000 y quedan $50,000: menos que el
    # mínimo del banco, así que el banco no entra y el remanente se declara.
    reparto = _optimizar(
        [sofipo_llena, banco_exigente], fiscal_2026, monto_total=Decimal("300000")
    )

    assert {c.producto_id for c in reparto.candidatos} == {sofipo_llena.producto_id}
    assert reparto.monto_no_asignado == Decimal("50000.00")


def test_an_institution_cap_cuts_a_segment_short(fiscal_2026: ParametrosFiscales) -> None:
    """El tope del emisor manda incluso a mitad de un tramo."""
    escalonada_sofipo = Candidato(
        producto_id=13,
        institucion_id=1300,
        tipo_seguro=TipoSeguro.PROSOFIPO,
        instrumento=TipoInstrumento.DEPOSITO_SOFIPO,
        tasa_nominal=Decimal("13.00"),
        plazo_dias=None,
        monto_minimo=Decimal("0"),
        tramos=(
            Tramo(desde=Decimal("0"), hasta=Decimal("300000"), tasa_nominal=Decimal("13.00")),
            Tramo(desde=Decimal("300000"), hasta=None, tasa_nominal=Decimal("6.00")),
        ),
    )
    reparto = _optimizar([escalonada_sofipo], fiscal_2026, monto_total=Decimal("400000"))

    # PROSOFIPO con UDI=10 cubre $250,000: el tramo de $300,000 se corta ahí.
    assert reparto.asignaciones == [(escalonada_sofipo, Decimal("250000.00"))]
    assert reparto.monto_no_asignado == Decimal("150000.00")


def test_a_rising_ladder_is_excluded_from_the_optimiser(
    fiscal_2026: ParametrosFiscales,
) -> None:
    """El greedy marginal solo es óptimo con escaleras no crecientes."""
    creciente = Candidato(
        producto_id=14,
        institucion_id=1400,
        tipo_seguro=TipoSeguro.IPAB,
        instrumento=TipoInstrumento.DEPOSITO_BANCARIO,
        tasa_nominal=Decimal("5.00"),
        plazo_dias=None,
        monto_minimo=Decimal("0"),
        tramos=(
            Tramo(desde=Decimal("0"), hasta=Decimal("30000"), tasa_nominal=Decimal("5.00")),
            Tramo(desde=Decimal("30000"), hasta=None, tasa_nominal=Decimal("9.00")),
        ),
    )
    reparto = _optimizar([creciente, CETES], fiscal_2026)

    assert creciente.producto_id not in {c.producto_id for c in reparto.candidatos}
    assert reparto.montos == [Decimal("100000.00")]


# ─── Pasos y descartes: el porqué del reparto ────────────────


def _razones(reparto) -> dict[int, RazonDescarte]:  # type: ignore[no-untyped-def]
    return {d.producto_id: d.razon for d in reparto.descartes}


def test_the_steps_add_up_to_the_allocations(fiscal_2026: ParametrosFiscales) -> None:
    """La suma de los pasos de cada producto es su asignación, al centavo."""
    monto = Decimal("5000000")
    reparto = _optimizar([CETES, BANCO, SOFIPO_A], fiscal_2026, monto_total=monto)

    por_producto: dict[int, Decimal] = {}
    for paso in reparto.pasos:
        por_producto[paso.producto_id] = (
            por_producto.get(paso.producto_id, Decimal("0")) + paso.monto
        )
    assert por_producto == {c.producto_id: m for c, m in reparto.asignaciones}
    assert sum(p.monto for p in reparto.pasos) == monto - reparto.monto_no_asignado


def test_first_appearance_in_steps_follows_the_opening_order(
    fiscal_2026: ParametrosFiscales,
) -> None:
    """Los pasos y las asignaciones cuentan el mismo orden de apertura."""
    reparto = _optimizar([CETES, BANCO, SOFIPO_A], fiscal_2026, monto_total=Decimal("5000000"))

    vistos: list[int] = []
    for paso in reparto.pasos:
        if paso.producto_id not in vistos:
            vistos.append(paso.producto_id)
    assert vistos == [c.producto_id for c in reparto.candidatos]


def test_a_funded_product_never_appears_in_the_discards(
    fiscal_2026: ParametrosFiscales,
) -> None:
    """El que llenó su tope cuenta su historia en el corte, no en descartes."""
    reparto = _optimizar([CETES, BANCO, SOFIPO_A], fiscal_2026, monto_total=Decimal("5000000"))

    fondeados = {c.producto_id for c in reparto.candidatos}
    assert fondeados & {d.producto_id for d in reparto.descartes} == set()
    ultimo_sofipo = [p for p in reparto.pasos if p.producto_id == SOFIPO_A.producto_id][-1]
    assert ultimo_sofipo.razon_corte is RazonCorte.LIMITE_SEGURO


def test_determinism_extends_to_steps_and_discards(fiscal_2026: ParametrosFiscales) -> None:
    uno = _optimizar([CETES, BANCO, SOFIPO_A, RIESGOSA], fiscal_2026)
    otro = _optimizar([CETES, BANCO, SOFIPO_A, RIESGOSA], fiscal_2026)

    assert uno.pasos == otro.pasos
    assert uno.descartes == otro.descartes


def test_crossing_products_reports_a_filled_tier_then_exhaustion(
    fiscal_2026: ParametrosFiscales,
) -> None:
    """El caso Openbank: el tramo alto se llena y el final agota el monto."""
    reparto = _optimizar([ESCALONADO, PLANO_MEDIO], fiscal_2026)

    assert [p.razon_corte for p in reparto.pasos] == [
        RazonCorte.TRAMO_LLENO,
        RazonCorte.MONTO_AGOTADO,
    ]
    assert reparto.pasos[0].producto_id == ESCALONADO.producto_id
    assert reparto.pasos[0].monto == Decimal("30000.00")
    assert reparto.pasos[0].tramo.tasa_nominal == Decimal("13.00")
    assert reparto.pasos[0].indice_tramo == 0


def test_a_minimum_entry_is_traced_with_its_blended_ten(
    fiscal_2026: ParametrosFiscales,
) -> None:
    """La entrada con mínimo cruza tramos: COMPRA_MINIMO y la TEN ponderada.

    Los números son los de `test_a_minimum_above_the_first_ceiling_buys_the
    _lower_tier_too`: entrar cuesta $50,000 al 10.32 % nominal, cuya TEN es
    9.42 % — eso, y no el 13 % del tramo alto, es lo que el paso enseña.
    """
    escalonado_con_minimo = Candidato(
        producto_id=30,
        institucion_id=3000,
        tipo_seguro=TipoSeguro.IPAB,
        instrumento=TipoInstrumento.DEPOSITO_BANCARIO,
        tasa_nominal=Decimal("13.00"),
        plazo_dias=None,
        monto_minimo=Decimal("50000"),
        tramos=ESCALERA_OPENBANK,
    )
    plano_diez = Candidato(
        producto_id=31,
        institucion_id=3100,
        tipo_seguro=TipoSeguro.IPAB,
        instrumento=TipoInstrumento.DEPOSITO_BANCARIO,
        tasa_nominal=Decimal("10.00"),
        plazo_dias=None,
        monto_minimo=Decimal("0"),
    )
    reparto = _optimizar(
        [escalonado_con_minimo, plano_diez], fiscal_2026, monto_total=Decimal("200000")
    )

    entrada = reparto.pasos[0]
    assert entrada.producto_id == escalonado_con_minimo.producto_id
    assert entrada.razon_corte is RazonCorte.COMPRA_MINIMO
    assert entrada.compra_minimo is True
    assert entrada.monto == Decimal("50000.00")
    assert entrada.ten_marginal == Decimal("9.4200")


def test_without_the_insurance_toggle_no_step_says_insurance(
    fiscal_2026: ParametrosFiscales,
) -> None:
    """Sin `respetar_seguro`, el tope ES el restante: el empate lo resuelve
    la cadena de prioridad y jamás se reporta «límite de seguro»."""
    reparto = _optimizar(
        [IFPE, CETES, SOFIPO_A],
        fiscal_2026,
        monto_total=Decimal("5000000"),
        respetar_seguro=False,
    )

    assert reparto.pasos
    assert all(p.razon_corte is not RazonCorte.LIMITE_SEGURO for p in reparto.pasos)


def test_each_discard_reason_names_what_actually_happened(
    fiscal_2026: ParametrosFiscales,
) -> None:
    """Las razones de descarte, montadas sobre los escenarios ya probados."""
    con_roja = _optimizar([RIESGOSA, CETES], fiscal_2026, excluir_rojas=True)
    assert _razones(con_roja)[RIESGOSA.producto_id] is RazonDescarte.BANDERA_ROJA

    corto = _optimizar([CETES, BANCO], fiscal_2026, horizonte_dias=91)
    assert _razones(corto)[CETES.producto_id] is RazonDescarte.PLAZO_MAYOR_AL_HORIZONTE

    chico = _optimizar(
        [RIESGOSA, BANCO], fiscal_2026, monto_total=Decimal("5000"), excluir_rojas=False
    )
    assert _razones(chico)[RIESGOSA.producto_id] is RazonDescarte.MINIMO_SUPERA_MONTO

    protegido = _optimizar([IFPE, CETES], fiscal_2026, respetar_seguro=True)
    assert _razones(protegido)[IFPE.producto_id] is RazonDescarte.SIN_COBERTURA

    creciente = Candidato(
        producto_id=32,
        institucion_id=3200,
        tipo_seguro=TipoSeguro.IPAB,
        instrumento=TipoInstrumento.DEPOSITO_BANCARIO,
        tasa_nominal=Decimal("5.00"),
        plazo_dias=None,
        monto_minimo=Decimal("0"),
        tramos=(
            Tramo(desde=Decimal("0"), hasta=Decimal("30000"), tasa_nominal=Decimal("5.00")),
            Tramo(desde=Decimal("30000"), hasta=None, tasa_nominal=Decimal("9.00")),
        ),
    )
    subiendo = _optimizar([creciente, CETES], fiscal_2026)
    assert _razones(subiendo)[creciente.producto_id] is RazonDescarte.ESCALERA_CRECIENTE

    sofipo_llena = Candidato(
        producto_id=33,
        institucion_id=3300,
        tipo_seguro=TipoSeguro.PROSOFIPO,
        instrumento=TipoInstrumento.DEPOSITO_SOFIPO,
        tasa_nominal=Decimal("12.00"),
        plazo_dias=None,
        monto_minimo=Decimal("0"),
    )
    banco_exigente = Candidato(
        producto_id=34,
        institucion_id=3400,
        tipo_seguro=TipoSeguro.IPAB,
        instrumento=TipoInstrumento.DEPOSITO_BANCARIO,
        tasa_nominal=Decimal("11.00"),
        plazo_dias=None,
        monto_minimo=Decimal("80000"),
    )
    apretado = _optimizar(
        [sofipo_llena, banco_exigente], fiscal_2026, monto_total=Decimal("300000")
    )
    assert _razones(apretado)[banco_exigente.producto_id] is RazonDescarte.MINIMO_INALCANZABLE


def test_evaluar_reparto_blends_tiered_candidates(fiscal_2026: ParametrosFiscales) -> None:
    """TEN y cascada de una asignación escalonada salen de la ponderada."""
    combinacion = evaluar_reparto(
        [ESCALONADO],
        [Decimal("50000")],
        horizonte_dias=HORIZONTE,
        inflacion_anual=Decimal("4.5"),
        params=fiscal_2026,
        valor_udi=UDI,
    )

    asignacion = combinacion.asignaciones[0]
    assert asignacion.cascada.tasa_nominal == Decimal("10.3200")
    assert asignacion.ten == Decimal("9.4200")
