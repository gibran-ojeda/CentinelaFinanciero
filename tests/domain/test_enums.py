"""Tests del vocabulario de dominio."""

from __future__ import annotations

import pytest

from domain.enums import (
    SEGURO_POR_CATEGORIA,
    CategoriaInstitucion,
    EstadoTasa,
    FuenteTasa,
    NivelCapitalizacion,
    Severidad,
    TipoInstrumento,
    TipoProducto,
    TipoSeguro,
)


def test_enums_are_string_valued_for_database_storage() -> None:
    """Se persisten como texto: el valor debe coincidir con el nombre."""
    for enum in (
        CategoriaInstitucion,
        TipoSeguro,
        TipoProducto,
        FuenteTasa,
        EstadoTasa,
        Severidad,
        TipoInstrumento,
        NivelCapitalizacion,
    ):
        for member in enum:
            assert member.value == member.name


def test_categoria_covers_every_regulatory_figure_of_section_3() -> None:
    assert set(CategoriaInstitucion) == {
        CategoriaInstitucion.GOBIERNO,
        CategoriaInstitucion.SOFIPO,
        CategoriaInstitucion.BANCO_DIGITAL,
        CategoriaInstitucion.BANCO_TRADICIONAL,
        CategoriaInstitucion.IFPE,
    }


@pytest.mark.parametrize(
    ("categoria", "seguro"),
    [
        (CategoriaInstitucion.GOBIERNO, TipoSeguro.SOBERANO),
        (CategoriaInstitucion.SOFIPO, TipoSeguro.PROSOFIPO),
        (CategoriaInstitucion.BANCO_DIGITAL, TipoSeguro.IPAB),
        (CategoriaInstitucion.BANCO_TRADICIONAL, TipoSeguro.IPAB),
        (CategoriaInstitucion.IFPE, TipoSeguro.NINGUNO),
    ],
)
def test_insurance_follows_the_regulatory_figure(
    categoria: CategoriaInstitucion, seguro: TipoSeguro
) -> None:
    """§4.6: la cobertura depende de la figura, no del nombre comercial.

    Un neobanco con licencia bancaria tiene IPAB aunque se sienta fintech; una
    SOFIPO tiene PROSOFIPO aunque capte más que un banco pequeño.
    """
    assert SEGURO_POR_CATEGORIA[categoria] is seguro


def test_every_category_maps_to_an_insurance_type() -> None:
    assert set(SEGURO_POR_CATEGORIA) == set(CategoriaInstitucion)


def test_pending_review_is_distinct_from_rejected() -> None:
    """Una tasa pendiente no se publica, pero tampoco está descartada."""
    assert EstadoTasa.PENDIENTE_REVISION is not EstadoTasa.RECHAZADA
    assert EstadoTasa.VIGENTE not in (EstadoTasa.PENDIENTE_REVISION, EstadoTasa.RECHAZADA)


def test_source_covers_the_three_data_levels_of_section_15() -> None:
    assert FuenteTasa.BANXICO_API in FuenteTasa  # nivel 1
    assert FuenteTasa.CNBV in FuenteTasa  # nivel 1
    assert FuenteTasa.FETCH_DIRIGIDO in FuenteTasa  # nivel 2
    assert FuenteTasa.LLM_RESEARCH in FuenteTasa  # nivel 3
    assert FuenteTasa.MANUAL in FuenteTasa  # MVP


def test_nicap_levels_follow_cnbv_categories() -> None:
    assert [n.value for n in NivelCapitalizacion] == ["N1", "N2", "N3", "N4"]
