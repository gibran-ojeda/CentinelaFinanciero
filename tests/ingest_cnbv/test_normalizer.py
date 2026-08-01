"""Tests del mapeo entre el nombre regulatorio y el del catálogo."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from domain.enums import CategoriaInstitucion
from ingest_cnbv import fuentes
from ingest_cnbv.normalizer import Candidata, MapeoAmbiguo, clave, mapear
from ingest_cnbv.parser import FilaInstitucion, combinar, leer_hoja

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "cnbv"
BANCA = FIXTURES / "banca_202605.xlsx"
SOFIPOS = FIXTURES / "sofipos_202603.xlsx"


def _fila(nombre: str, **valores: Decimal | str | None) -> FilaInstitucion:
    return FilaInstitucion(nombre_cnbv=nombre, valores=dict(valores))


def _sofipo(id_: int, nombre: str, nombre_cnbv: str | None) -> Candidata:
    return Candidata(
        id=id_, nombre=nombre, nombre_cnbv=nombre_cnbv, categoria=CategoriaInstitucion.SOFIPO
    )


def _banco(id_: int, nombre: str, nombre_cnbv: str | None) -> Candidata:
    return Candidata(
        id=id_,
        nombre=nombre,
        nombre_cnbv=nombre_cnbv,
        categoria=CategoriaInstitucion.BANCO_DIGITAL,
    )


# ─── La clave ─────────────────────────────────────────────────


def test_the_same_bank_written_two_ways_gets_one_key() -> None:
    """La CNBV escribe «Ualá» en una hoja y «Banco Ualá» en otra."""
    assert clave("Ualá") == clave("Banco Ualá")
    assert clave("Openbank") == clave("Openbank México")


def test_distinct_institutions_keep_distinct_keys() -> None:
    """La normalización recorta decoraciones, no distingue de menos."""
    distintas = ["Banco Azteca", "Banco Base", "BanCoppel", "Banco del Bajío", "Banregio"]

    assert len({clave(n) for n in distintas}) == len(distintas)


def test_a_decoration_that_would_empty_the_name_is_not_stripped() -> None:
    """Si sólo quedara «banco», recortar dejaría la clave vacía."""
    assert clave("Banco") != ""


# ─── El cruce ─────────────────────────────────────────────────


def test_a_bank_named_differently_across_sheets_keeps_all_its_data() -> None:
    """Sin fundir por clave, Ualá se quedaría sin ICAP y nadie lo notaría.

    La columna simplemente vendría nula, que es un valor legítimo — el peor
    tipo de fallo.
    """
    lectura = combinar(
        *[leer_hoja(BANCA, hoja, periodo=date(2026, 5, 31)) for hoja in fuentes.HOJAS_BANCA]
    )
    candidatas = [_banco(1, "Ualá", "Ualá")]

    reporte = mapear(candidatas, lectura, categorias=fuentes.BOLETIN_BANCA.categorias)

    fila = reporte.casadas[1]
    assert fila.numero("imor") is not None  # viene de la hoja CCT
    assert fila.numero("icap") is not None  # viene de Art_121, con otro nombre


def test_the_real_catalogue_maps_against_the_real_bulletin() -> None:
    lectura = combinar(
        *[leer_hoja(SOFIPOS, hoja, periodo=date(2026, 3, 31)) for hoja in fuentes.HOJAS_SOFIPO]
    )
    candidatas = [
        _sofipo(1, "Finsus", "Financiera Sustentable"),
        _sofipo(2, "kubo.financiero", "KU-BO"),
        _sofipo(3, "Libertad Servicios Financieros", "Libertad"),
    ]

    reporte = mapear(candidatas, lectura, categorias=fuentes.BOLETIN_SOFIPO.categorias)

    assert set(reporte.casadas) == {1, 2, 3}
    assert reporte.casadas[1].numero("imor") == Decimal("1.89")


def test_an_institution_without_nombre_cnbv_is_reported_not_guessed() -> None:
    """DiDi, Supertasas y Te Creemos no aparecen con nombre reconocible.

    Casarlas por parecido sería cargar los indicadores de otra sociedad, que es
    peor que dejarlas sin datos.
    """
    reporte = mapear(
        [_sofipo(7, "Supertasas", None)],
        {"Operaciones de tu Lado": _fila("Operaciones de tu Lado", imor=Decimal("4"))},
        categorias=fuentes.BOLETIN_SOFIPO.categorias,
    )

    assert reporte.sin_mapear == ["Supertasas"]
    assert reporte.casadas == {}


def test_an_institution_absent_from_this_bulletin_is_reported() -> None:
    """En figura y ausente: eso sí es un hueco que alguien tiene que mirar."""
    reporte = mapear(
        [_banco(9, "Nu México", "Nu México")],
        {"BBVA México": _fila("BBVA México", imor=Decimal("1.6"))},
        categorias=fuentes.BOLETIN_BANCA.categorias,
    )

    assert reporte.sin_datos == ["Nu México"]
    assert reporte.fuera_del_catalogo == 1


def test_two_catalogue_institutions_sharing_a_key_is_an_error() -> None:
    """Mezclar los indicadores de dos entidades es peor que no cargarlos."""
    with pytest.raises(MapeoAmbiguo, match="misma clave"):
        mapear(
            [_banco(1, "Ualá", "Ualá"), _banco(2, "Ualá bis", "Banco Ualá")],
            {},
            categorias=fuentes.BOLETIN_BANCA.categorias,
        )


def test_merging_never_overwrites_a_value_with_a_gap() -> None:
    """La hoja que no publica un concepto trae `None`; la que sí, un número."""
    reporte = mapear(
        [_banco(1, "Ualá", "Ualá")],
        {
            "Ualá": _fila("Ualá", imor=Decimal("10.6"), icap=None),
            "Banco Ualá": _fila("Banco Ualá", imor=None, icap=Decimal("24.85")),
        },
        categorias=fuentes.BOLETIN_BANCA.categorias,
    )

    fila = reporte.casadas[1]
    assert fila.numero("imor") == Decimal("10.6")
    assert fila.numero("icap") == Decimal("24.85")


# ─── El alcance del reporte ───────────────────────────────────


def test_the_report_is_scoped_to_the_bulletins_figures() -> None:
    """El Gobierno Federal no va a estar en ningún boletín.

    Reportarlo cada corrida como «sin nombre_cnbv» —y a cada banco como «sin
    datos» en el boletín de SOFIPOs— es ruido permanente en la única señal
    que significa «alguien tiene que mapear esto».
    """
    reporte = mapear(
        [
            Candidata(
                id=1,
                nombre="Gobierno Federal",
                nombre_cnbv=None,
                categoria=CategoriaInstitucion.GOBIERNO,
            ),
            _banco(2, "Nu México", "Nu México"),
        ],
        {"Klar": _fila("Klar", imor=Decimal("4"))},
        categorias=fuentes.BOLETIN_SOFIPO.categorias,
    )

    assert reporte.sin_mapear == []  # el Gobierno no es figura de este boletín
    assert reporte.sin_datos == []  # y que Nu no esté aquí es lo normal


def test_an_out_of_figure_match_still_maps() -> None:
    """El caso Nu, fijado como regresión.

    La CNBV lo publica entre las SOFIPOs aunque el catálogo lo tenga como
    banco digital. Se acota el **reporte**, jamás el **casamiento**: filtrar
    candidatas por figura lo habría dejado sin indicadores.
    """
    reporte = mapear(
        [_banco(9, "Nu México", "Nu México")],
        {"Nu México": _fila("Nu México", imor=Decimal("3.1"))},
        categorias=fuentes.BOLETIN_SOFIPO.categorias,
    )

    assert 9 in reporte.casadas
    assert reporte.casadas[9].numero("imor") == Decimal("3.1")
