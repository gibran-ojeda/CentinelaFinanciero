"""Tests del lector del PDF de capitalización de SOFIPOs.

El fixture es `ICAP_SOFIPOS_202605.pdf` **sin recortar**: pesa 143 KB y es el
archivo tal cual lo publica la CNBV, que para un formato tan frágil como un PDF
vale más que cualquier reducción.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ingest_cnbv.nicap import leer_nicap
from ingest_cnbv.parser import FormatoInesperado

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "cnbv"
NICAP = FIXTURES / "nicap_sofipos_202605.pdf"
PERIODO = date(2026, 5, 31)


def _por_nombre() -> dict[str, object]:
    return {n.nombre_cnbv: n for n in leer_nicap(NICAP, periodo=PERIODO)}


def test_reads_every_society_in_the_document() -> None:
    """El propio PDF dice cuántas hay: «se encontraban en operación 35»."""
    niveles = leer_nicap(NICAP, periodo=PERIODO)

    assert len(niveles) == 35


def test_the_capitalisation_level_becomes_an_n_category() -> None:
    """La columna «categoría» es 1–4 y el dominio la llama N1–N4."""
    niveles = _por_nombre()

    assert niveles["KU-BO"].nivel == "N1"  # type: ignore[attr-defined]
    assert niveles["Nu México"].nivel == "N1"  # type: ignore[attr-defined]
    assert niveles["Stori"].nivel == "N1"  # type: ignore[attr-defined]


def test_under_review_is_absence_and_never_the_worst_level() -> None:
    """`n.d.` es «la CNBV lo está revisando», no «mal capitalizada».

    Cuatro sociedades salen así en mayo de 2026. Traducirlo a N4 pintaría una
    bandera roja por un trámite administrativo.
    """
    niveles = _por_nombre()

    for nombre in ("Crediclub", "Libertad", "Opciones Empresariales", "Acción y Evolución"):
        assert niveles[nombre].nivel is None, nombre  # type: ignore[attr-defined]


def test_footnote_markers_are_stripped_from_the_name() -> None:
    """El PDF escribe `Crediclub3/`, y el nombre es la clave de mapeo."""
    nombres = set(_por_nombre())

    assert "Crediclub" in nombres
    assert not any(n.endswith("/") for n in nombres)


def test_the_percentage_is_read_despite_the_pdf_kerning() -> None:
    """`8 4,869,859` es 84,869,859: el PDF mete espacios dentro de los números."""
    niveles = _por_nombre()

    assert niveles["KU-BO"].porcentaje == Decimal("280")  # type: ignore[attr-defined]
    assert niveles["Ictineo"].porcentaje == Decimal("14453")  # type: ignore[attr-defined]


def test_the_casfim_key_is_kept() -> None:
    """Es el identificador regulatorio, más estable que cualquier nombre."""
    niveles = _por_nombre()

    assert niveles["Nu México"].clave_casfim == "027014"  # type: ignore[attr-defined]


def test_the_sector_total_is_not_a_society() -> None:
    nombres = set(_por_nombre())

    assert not any(n.lower().startswith("total") for n in nombres)


# ─── Fallo ruidoso ────────────────────────────────────────────


def test_the_wrong_period_fails_loudly() -> None:
    """Se comprueba contra el «CIFRAS AL» del documento, no contra el nombre."""
    with pytest.raises(FormatoInesperado, match="2026-05"):
        leer_nicap(NICAP, periodo=date(2026, 3, 31))


def test_another_document_fails_loudly(tmp_path: Path) -> None:
    otro = tmp_path / "otro.pdf"
    otro.write_bytes(b"%PDF-1.4\n% no soy el reporte\n")

    with pytest.raises(FormatoInesperado):
        leer_nicap(otro, periodo=PERIODO)


def test_a_file_that_is_not_a_pdf_fails_loudly(tmp_path: Path) -> None:
    falso = tmp_path / "no.pdf"
    falso.write_bytes(b"esto no es un pdf")

    with pytest.raises(FormatoInesperado, match="no se pudo abrir"):
        leer_nicap(falso, periodo=PERIODO)
