"""Tests del lector de boletines, contra los ficheros reales de la CNBV.

`tests/fixtures/cnbv/` son recortes de `BE BM 202605.xlsx` y
`BE_SOFIPOS_202603.xlsx` con las mismas hojas, los mismos encabezados y los
mismos valores — menos instituciones, nada más. El criterio de §8 pide
boletines reales y no sintéticos, y el recorte es reproducible con
`construye_fixtures.py`.

Los casos de formato roto sí se arman a mano: para probar que el parser se
rompe ruidosamente hace falta un archivo roto, y la CNBV no publica uno.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook

from ingest_cnbv import fuentes
from ingest_cnbv.parser import FormatoInesperado, combinar, leer_hoja

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "cnbv"
BANCA = FIXTURES / "banca_202605.xlsx"
SOFIPOS = FIXTURES / "sofipos_202603.xlsx"

PERIODO_BANCA = date(2026, 5, 31)
PERIODO_SOFIPO = date(2026, 3, 31)


# ─── Banca múltiple ───────────────────────────────────────────


def test_reads_the_current_period_and_not_the_year_before() -> None:
    """El bloque de IMOR abre en la columna F, pero esa F es de hace un año.

    Es el fallo que más caro habría salido: publicar la morosidad de mayo de
    2025 como si fuera la de mayo de 2026, en silencio y con las banderas
    moviéndose detrás.
    """
    filas = {
        f.nombre_cnbv: f for f in leer_hoja(BANCA, fuentes.BANCA_CARTERA, periodo=PERIODO_BANCA)
    }

    # A cuatro decimales, que es la precisión con la que `tasas` y
    # `indicadores_financieros` guardan un porcentaje. Con más, el test mediría
    # cómo openpyxl reserializa un float al construir el fixture y no lo que
    # el parser lee.
    imor = filas["BBVA México"].numero("imor")
    assert imor is not None
    assert round(imor, 4) == Decimal("1.6777")  # mayo de 2026
    assert round(imor, 4) != Decimal("1.6467")  # mayo de 2025, la columna F


def test_reads_capital_and_deposits_of_a_digital_bank() -> None:
    lectura = combinar(
        *[leer_hoja(BANCA, hoja, periodo=PERIODO_BANCA) for hoja in fuentes.HOJAS_BANCA]
    )

    revolut = lectura["Revolut Bank"]
    assert revolut.numero("icap") == Decimal("166.0869207")
    assert revolut.texto("categoria") == "I"
    assert revolut.numero("captacion") is not None


def test_the_system_aggregate_is_not_an_institution() -> None:
    """La fila «Sistema */» es la suma del sector, no un banco más."""
    nombres = {
        f.nombre_cnbv for f in leer_hoja(BANCA, fuentes.BANCA_CARTERA, periodo=PERIODO_BANCA)
    }

    assert not any(n.lower().startswith("sistema") for n in nombres)


def test_footnotes_are_not_read_as_institutions() -> None:
    """`Art_121` cierra con cinco notas numeradas en la columna de nombres."""
    nombres = [
        f.nombre_cnbv for f in leer_hoja(BANCA, fuentes.BANCA_CAPITAL, periodo=PERIODO_BANCA)
    ]

    assert not any(n.startswith(("1/", "2/", "3/", "4/", "5/")) for n in nombres)
    assert all(len(n) <= 60 for n in nombres)


# ─── SOFIPOs ──────────────────────────────────────────────────


def test_reads_the_sofipo_bulletin() -> None:
    lectura = combinar(
        *[leer_hoja(SOFIPOS, hoja, periodo=PERIODO_SOFIPO) for hoja in fuentes.HOJAS_SOFIPO]
    )

    # Valores reales de marzo de 2026.
    assert lectura["Nu México"].numero("imor") == Decimal("5.74")
    assert lectura["Libertad"].numero("imor") == Decimal("44.37")
    assert lectura["Financiera Sustentable"].numero("imor") == Decimal("1.89")


def test_na_is_absence_and_not_zero() -> None:
    """CAME no reportó en marzo de 2026. Un cero diría que no tiene morosidad."""
    lectura = combinar(
        *[leer_hoja(SOFIPOS, hoja, periodo=PERIODO_SOFIPO) for hoja in fuentes.HOJAS_SOFIPO]
    )

    came = lectura["CAME"]
    assert came.numero("imor") is None
    assert came.numero("activo_total") is None


def test_the_sector_total_is_not_an_institution() -> None:
    nombres = {
        f.nombre_cnbv for f in leer_hoja(SOFIPOS, fuentes.SOFIPO_CARTERA, periodo=PERIODO_SOFIPO)
    }

    assert "Total del sector" not in nombres


# ─── Unidades ─────────────────────────────────────────────────


def test_both_bulletins_end_up_in_pesos() -> None:
    """Banca publica en millones y SOFIPOs en miles.

    Cargar sin convertir dejaría la captación de un banco mil veces por debajo
    de la de una SOFIPO, y el comparador las pone en la misma columna. Es un
    error que no se ve: los dos números son plausibles por separado.
    """
    banca = combinar(
        *[leer_hoja(BANCA, hoja, periodo=PERIODO_BANCA) for hoja in fuentes.HOJAS_BANCA]
    )
    sofipos = combinar(
        *[leer_hoja(SOFIPOS, hoja, periodo=PERIODO_SOFIPO) for hoja in fuentes.HOJAS_SOFIPO]
    )

    # BBVA: 2,246,386.53 millones de pesos en el boletín.
    bbva = banca["BBVA México"].numero("captacion")
    assert bbva is not None
    assert Decimal("2.2e12") < bbva < Decimal("2.3e12")

    # Nu México: 105,918,459.46 miles de pesos.
    nu = sofipos["Nu México"].numero("captacion")
    assert nu is not None
    assert Decimal("1.05e11") < nu < Decimal("1.06e11")


def test_percentages_are_not_scaled() -> None:
    lectura = combinar(
        *[leer_hoja(SOFIPOS, hoja, periodo=PERIODO_SOFIPO) for hoja in fuentes.HOJAS_SOFIPO]
    )

    assert lectura["Libertad"].numero("imor") == Decimal("44.37")


# ─── Fallo ruidoso ────────────────────────────────────────────


def test_a_period_the_bulletin_does_not_have_fails_loudly() -> None:
    with pytest.raises(FormatoInesperado) as exc:
        leer_hoja(BANCA, fuentes.BANCA_CARTERA, periodo=date(2020, 1, 31))

    # El mensaje dice qué periodos sí encontró: sin eso, quien lo lea no sabe
    # si el archivo está mal o la fecha que pidió.
    assert "2026-05" in str(exc.value)


def test_a_missing_sheet_fails_loudly() -> None:
    with pytest.raises(FormatoInesperado, match="no tiene la hoja"):
        leer_hoja(SOFIPOS, fuentes.BANCA_CARTERA, periodo=PERIODO_SOFIPO)


def test_a_renamed_column_fails_loudly(tmp_path: Path) -> None:
    """Si la CNBV mueve una columna, el job se rompe. Nunca lee la de al lado."""
    libro = Workbook()
    hoja = libro.active
    assert hoja is not None
    hoja.title = fuentes.BANCA_CARTERA.nombre
    for _ in range(4):
        hoja.append([])
    hoja.append([None, None, "Cartera total", None, None, "Otra cosa cualquiera"])
    hoja.append([None, None, date(2026, 5, 1), None, None, date(2026, 5, 1)])
    hoja.append([None, "BBVA México", 1, None, None, 2])
    destino = tmp_path / "roto.xlsx"
    libro.save(destino)

    with pytest.raises(FormatoInesperado, match="IMOR"):
        leer_hoja(destino, fuentes.BANCA_CARTERA, periodo=PERIODO_BANCA)


def test_a_file_that_is_not_ooxml_fails_loudly(tmp_path: Path) -> None:
    """Los boletines anteriores a 2016 vienen en `.xls`, que no se lee aquí."""
    falso = tmp_path / "viejo.xls"
    falso.write_bytes(b"\xd0\xcf\x11\xe0 no soy un zip")

    with pytest.raises(FormatoInesperado, match="OOXML"):
        leer_hoja(falso, fuentes.BANCA_CARTERA, periodo=PERIODO_BANCA)


def test_an_empty_sheet_fails_instead_of_loading_nothing(tmp_path: Path) -> None:
    libro = Workbook()
    hoja = libro.active
    assert hoja is not None
    hoja.title = fuentes.BANCA_CAPITAL.nombre
    for _ in range(7):
        hoja.append([])
    hoja.append([None, "Institución", None, None, "ICAP", "Categoría"])
    destino = tmp_path / "vacio.xlsx"
    libro.save(destino)

    with pytest.raises(FormatoInesperado, match="ninguna institución"):
        leer_hoja(destino, fuentes.BANCA_CAPITAL, periodo=PERIODO_BANCA)


# ─── Unión de hojas ───────────────────────────────────────────


def test_combining_sheets_puts_every_concept_on_one_row() -> None:
    lectura = combinar(
        *[leer_hoja(BANCA, hoja, periodo=PERIODO_BANCA) for hoja in fuentes.HOJAS_BANCA]
    )

    hey = lectura["Hey Banco"]
    assert {"imor", "icor", "icap", "categoria", "captacion"} <= set(hey.valores)
