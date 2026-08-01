"""Tests del mapeo entre el nombre regulatorio y el del catálogo."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ingest_cnbv import fuentes
from ingest_cnbv.normalizer import Candidata, MapeoAmbiguo, clave, mapear
from ingest_cnbv.parser import FilaInstitucion, combinar, leer_hoja

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "cnbv"
BANCA = FIXTURES / "banca_202605.xlsx"
SOFIPOS = FIXTURES / "sofipos_202603.xlsx"


def _fila(nombre: str, **valores: Decimal | str | None) -> FilaInstitucion:
    return FilaInstitucion(nombre_cnbv=nombre, valores=dict(valores))


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
    candidatas = [Candidata(id=1, nombre="Ualá", nombre_cnbv="Ualá")]

    reporte = mapear(candidatas, lectura)

    fila = reporte.casadas[1]
    assert fila.numero("imor") is not None  # viene de la hoja CCT
    assert fila.numero("icap") is not None  # viene de Art_121, con otro nombre


def test_the_real_catalogue_maps_against_the_real_bulletin() -> None:
    lectura = combinar(
        *[leer_hoja(SOFIPOS, hoja, periodo=date(2026, 3, 31)) for hoja in fuentes.HOJAS_SOFIPO]
    )
    candidatas = [
        Candidata(id=1, nombre="Finsus", nombre_cnbv="Financiera Sustentable"),
        Candidata(id=2, nombre="kubo.financiero", nombre_cnbv="KU-BO"),
        Candidata(id=3, nombre="Libertad Servicios Financieros", nombre_cnbv="Libertad"),
    ]

    reporte = mapear(candidatas, lectura)

    assert set(reporte.casadas) == {1, 2, 3}
    assert reporte.casadas[1].numero("imor") == Decimal("1.89")


def test_an_institution_without_nombre_cnbv_is_reported_not_guessed() -> None:
    """DiDi, Supertasas y Te Creemos no aparecen con nombre reconocible.

    Casarlas por parecido sería cargar los indicadores de otra sociedad, que es
    peor que dejarlas sin datos.
    """
    reporte = mapear(
        [Candidata(id=7, nombre="Supertasas", nombre_cnbv=None)],
        {"Operaciones de tu Lado": _fila("Operaciones de tu Lado", imor=Decimal("4"))},
    )

    assert reporte.sin_mapear == ["Supertasas"]
    assert reporte.casadas == {}


def test_an_institution_absent_from_this_bulletin_is_reported() -> None:
    reporte = mapear(
        [Candidata(id=9, nombre="Nu México", nombre_cnbv="Nu México")],
        {"BBVA México": _fila("BBVA México", imor=Decimal("1.6"))},
    )

    assert reporte.sin_datos == ["Nu México"]
    assert reporte.fuera_del_catalogo == 1


def test_two_catalogue_institutions_sharing_a_key_is_an_error() -> None:
    """Mezclar los indicadores de dos entidades es peor que no cargarlos."""
    with pytest.raises(MapeoAmbiguo, match="misma clave"):
        mapear(
            [
                Candidata(id=1, nombre="Ualá", nombre_cnbv="Ualá"),
                Candidata(id=2, nombre="Ualá bis", nombre_cnbv="Banco Ualá"),
            ],
            {},
        )


def test_merging_never_overwrites_a_value_with_a_gap() -> None:
    """La hoja que no publica un concepto trae `None`; la que sí, un número."""
    reporte = mapear(
        [Candidata(id=1, nombre="Ualá", nombre_cnbv="Ualá")],
        {
            "Ualá": _fila("Ualá", imor=Decimal("10.6"), icap=None),
            "Banco Ualá": _fila("Banco Ualá", imor=None, icap=Decimal("24.85")),
        },
    )

    fila = reporte.casadas[1]
    assert fila.numero("imor") == Decimal("10.6")
    assert fila.numero("icap") == Decimal("24.85")
