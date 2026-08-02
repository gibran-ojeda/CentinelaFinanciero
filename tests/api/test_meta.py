"""Tests del endpoint de frescura."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient

from api.routers.meta import SLA_POR_FUENTE
from core.frescura import FUENTES_INFORMATIVAS
from domain.enums import FuenteTasa

pytestmark = [pytest.mark.requires_docker, pytest.mark.usefixtures("real_db")]


async def test_requires_authentication(api: AsyncClient) -> None:
    assert (await api.get("/api/v1/meta/frescura")).status_code == 401


async def test_reports_every_source_even_without_data(api_lectura: AsyncClient) -> None:
    """La UI necesita la lista completa para no inventar huecos."""
    respuesta = await api_lectura.get("/api/v1/meta/frescura")

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    esperadas = {f.value for f in SLA_POR_FUENTE} | {f.value for f in FUENTES_INFORMATIVAS}
    assert {f["fuente"] for f in cuerpo["fuentes"]} == esperadas


async def test_a_source_without_data_is_not_out_of_sla(api_lectura: AsyncClient) -> None:
    """Sin datos no hay retraso: marcarla en rojo sería alarma sin causa."""
    cuerpo = (await api_lectura.get("/api/v1/meta/frescura")).json()

    banxico = next(f for f in cuerpo["fuentes"] if f["fuente"] == FuenteTasa.BANXICO_API)
    assert banxico["observaciones"] == 0
    assert banxico["ultima_actualizacion"] is None
    assert banxico["dentro_de_sla"] is True
    assert cuerpo["todo_dentro_de_sla"] is True


@pytest.mark.usefixtures("catalogo_cargado")
async def test_reports_the_latest_date_and_the_count(api_lectura: AsyncClient) -> None:
    cuerpo = (await api_lectura.get("/api/v1/meta/frescura")).json()

    # Cinco: las gubernamentales verificadas contra fuente primaria. Las otras
    # treinta del catálogo son de agregador y no cuentan.
    manual = next(f for f in cuerpo["fuentes"] if f["fuente"] == FuenteTasa.MANUAL)
    assert manual["observaciones"] == 5
    assert manual["ultima_actualizacion"] == "2026-07-25"
    # Informativa: se reporta con fecha y conteo, sin SLA que la ponga en rojo.
    assert manual["sla_dias"] is None
    assert manual["dentro_de_sla"] is True


@pytest.mark.usefixtures("catalogo_cargado")
async def test_the_aggregator_source_is_not_in_the_freshness_report(
    api_lectura: AsyncClient,
) -> None:
    """Este endpoint mide la frescura de lo que se sirve.

    Un dato de agregador no se sirve nunca, así que una fila suya en rojo sería
    una alarma sobre algo que no llega a ningún usuario.
    """
    cuerpo = (await api_lectura.get("/api/v1/meta/frescura")).json()

    assert FuenteTasa.AGREGADOR not in {f["fuente"] for f in cuerpo["fuentes"]}
    assert FuenteTasa.AGREGADOR not in SLA_POR_FUENTE


@pytest.mark.usefixtures("catalogo_cargado")
async def test_stale_data_is_flagged(api_lectura: AsyncClient, tmp_path: Path) -> None:
    """Un dato manual de hace más de diez días sale del SLA."""
    from cli.tasas import import_csv

    vieja = (date.today() - timedelta(days=60)).isoformat()
    ruta = tmp_path / "tasas_viejas.csv"
    ruta.write_text(
        "producto_slug,tasa_nominal,fecha_dato,fuente\n" f"cetes-28,6.29,{vieja},BANXICO_API\n",
        encoding="utf-8",
    )
    await import_csv(ruta)

    cuerpo = (await api_lectura.get("/api/v1/meta/frescura")).json()
    banxico = next(f for f in cuerpo["fuentes"] if f["fuente"] == FuenteTasa.BANXICO_API)

    assert banxico["dias_desde_actualizacion"] == 60
    assert banxico["dentro_de_sla"] is False
    assert cuerpo["todo_dentro_de_sla"] is False


def test_slas_reflect_each_sources_real_cadence() -> None:
    """Banxico publica diario; la CNBV, con uno a tres meses de rezago."""
    assert SLA_POR_FUENTE[FuenteTasa.BANXICO_API] < SLA_POR_FUENTE[FuenteTasa.FETCH_DIRIGIDO]
    assert SLA_POR_FUENTE[FuenteTasa.FETCH_DIRIGIDO] < SLA_POR_FUENTE[FuenteTasa.CNBV]
    assert SLA_POR_FUENTE[FuenteTasa.CNBV] >= 90


def test_sources_without_a_cadence_carry_no_sla() -> None:
    """MANUAL es corrección puntual y LLM_RESEARCH descubrimiento oportunista.

    Exigirles una cadencia pondría sus filas en rojo permanente en cuanto las
    fuentes automáticas las superseden — la alarma que se aprende a ignorar.
    """
    assert set(FUENTES_INFORMATIVAS) == {FuenteTasa.MANUAL, FuenteTasa.LLM_RESEARCH}
    assert not set(FUENTES_INFORMATIVAS) & set(SLA_POR_FUENTE)
