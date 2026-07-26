"""Tests de los modelos pydantic de dominio y sus puentes desde el ORM."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from domain import orm
from domain.enums import (
    CategoriaInstitucion,
    EstadoTasa,
    FuenteTasa,
    Liquidez,
    TipoInstrumento,
    TipoProducto,
    TipoSeguro,
)
from domain.models import (
    IndicadoresInstitucion,
    ParametrosFiscales,
    Producto,
    Tasa,
    UmbralesBanderas,
    from_orm_indicadores,
    from_orm_institucion,
    from_orm_producto,
    from_orm_tasa,
)


def test_domain_models_are_immutable() -> None:
    """Un modelo de dominio no se muta, se reemplaza."""
    tasa = Tasa(
        id=1,
        producto_id=1,
        tasa_nominal=Decimal("9.5"),
        fecha_dato=date(2026, 7, 25),
        fuente=FuenteTasa.MANUAL,
    )
    with pytest.raises(ValidationError):
        tasa.tasa_nominal = Decimal("99")  # type: ignore[misc]


def test_rate_requires_provenance_and_date() -> None:
    """§11 y §19: ninguna tasa existe sin fecha y fuente."""
    with pytest.raises(ValidationError):
        Tasa(id=1, producto_id=1, tasa_nominal=Decimal("9.5"))  # type: ignore[call-arg]


def test_only_current_rates_are_publishable() -> None:
    def _tasa(estado: EstadoTasa) -> Tasa:
        return Tasa(
            id=1,
            producto_id=1,
            tasa_nominal=Decimal("9.5"),
            fecha_dato=date(2026, 7, 25),
            fuente=FuenteTasa.LLM_RESEARCH,
            estado=estado,
        )

    assert _tasa(EstadoTasa.VIGENTE).publicable is True
    assert _tasa(EstadoTasa.PENDIENTE_REVISION).publicable is False
    assert _tasa(EstadoTasa.RECHAZADA).publicable is False


def _producto(**kwargs: object) -> Producto:
    defaults: dict[str, object] = {
        "id": 1,
        "institucion_id": 1,
        "nombre": "Ahorro",
        "slug": "ahorro",
        "tipo": TipoProducto.PLAZO,
        "instrumento": TipoInstrumento.DEPOSITO_SOFIPO,
        "plazo_dias": 91,
        "monto_minimo": Decimal("100"),
        "liquidez": Liquidez.AL_VENCIMIENTO,
    }
    defaults.update(kwargs)
    return Producto(**defaults)  # type: ignore[arg-type]


def test_term_product_uses_its_own_horizon() -> None:
    assert _producto(plazo_dias=91).plazo_efectivo_dias == 91


def test_sight_product_annualizes_over_a_full_year() -> None:
    """Un producto a la vista no tiene plazo contractual, pero sí horizonte."""
    vista = _producto(tipo=TipoProducto.VISTA, plazo_dias=None, liquidez=Liquidez.INMEDIATA)
    assert vista.plazo_efectivo_dias == 365


def test_leverage_is_none_without_the_inputs() -> None:
    """La CNBV no publica todo para todas las figuras: no se inventa nada."""
    base = IndicadoresInstitucion(institucion_id=1, periodo=date(2026, 3, 31))
    assert base.apalancamiento is None
    assert base.imor is None

    solo_pasivo = base.model_copy(update={"pasivo_total": Decimal("1000")})
    assert solo_pasivo.apalancamiento is None


def test_leverage_is_the_liability_to_equity_ratio() -> None:
    indicadores = IndicadoresInstitucion(
        institucion_id=1,
        periodo=date(2026, 3, 31),
        pasivo_total=Decimal("1200"),
        capital_contable=Decimal("100"),
    )
    assert indicadores.apalancamiento == Decimal("12")


def test_zero_equity_does_not_divide_by_zero() -> None:
    indicadores = IndicadoresInstitucion(
        institucion_id=1,
        periodo=date(2026, 3, 31),
        pasivo_total=Decimal("1200"),
        capital_contable=Decimal("0"),
    )
    assert indicadores.apalancamiento is None


def test_threshold_defaults_match_the_foundation() -> None:
    """§5.1: los defaults son los umbrales del documento."""
    u = UmbralesBanderas()
    assert (u.imor_amarilla, u.imor_roja) == (Decimal("3.0"), Decimal("6.0"))
    assert (u.icap_amarilla, u.icap_roja) == (Decimal("15.0"), Decimal("10.5"))
    assert (u.cobertura_amarilla, u.cobertura_roja) == (Decimal("100.0"), Decimal("70.0"))
    assert u.gat_inconsistencia_pp == Decimal("1.5")


def test_fiscal_note_states_rate_and_reference_date() -> None:
    """§6 obliga a mostrar qué retención se aplicó y desde cuándo."""
    nota = ParametrosFiscales(
        anio=2026,
        tasa_retencion_capital=Decimal("0.50"),
        vigente_desde=date(2026, 1, 1),
    ).nota_fiscal
    assert "0.50" in nota
    assert "2026-01-01" in nota
    assert "capital" in nota


# ─── Puentes desde el ORM ─────────────────────────────────────


def test_bridges_map_orm_rows_without_a_session() -> None:
    """Los mappers no tocan la base: `metrics/` debe poder testearse sin ella."""
    institucion = orm.Institucion(
        id=7,
        nombre="FinSUS",
        slug="finsus",
        categoria=CategoriaInstitucion.SOFIPO,
        tipo_seguro=TipoSeguro.PROSOFIPO,
        activa=True,
    )
    dominio = from_orm_institucion(institucion)
    assert dominio.id == 7
    assert dominio.categoria is CategoriaInstitucion.SOFIPO

    producto = orm.Producto(
        id=3,
        institucion_id=7,
        nombre="Ahorro 91",
        slug="finsus-91",
        tipo=TipoProducto.PLAZO,
        instrumento=TipoInstrumento.DEPOSITO_SOFIPO,
        plazo_dias=91,
        monto_minimo=Decimal("100"),
        liquidez=Liquidez.AL_VENCIMIENTO,
        activo=True,
    )
    assert from_orm_producto(producto).plazo_efectivo_dias == 91

    tasa = orm.Tasa(
        id=11,
        producto_id=3,
        tasa_nominal=Decimal("9.75"),
        fecha_dato=date(2026, 7, 25),
        fuente=FuenteTasa.MANUAL,
        estado=EstadoTasa.VIGENTE,
    )
    assert from_orm_tasa(tasa).publicable is True


def test_indicator_bridge_accepts_a_derived_growth_rate() -> None:
    """El crecimiento de captación se calcula, no viene de la CNBV."""
    fila = orm.IndicadorFinanciero(
        institucion_id=7,
        periodo=date(2026, 3, 31),
        imor=Decimal("4.2"),
        icap=Decimal("12.0"),
    )
    dominio = from_orm_indicadores(fila, crecimiento_captacion_pct=Decimal("80"))
    assert dominio.imor == Decimal("4.2")
    assert dominio.crecimiento_captacion_pct == Decimal("80")
