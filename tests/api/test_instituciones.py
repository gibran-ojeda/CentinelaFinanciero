"""Tests del detalle de institución."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from core.db import session_scope
from domain.enums import NivelCapitalizacion, Severidad, TipoBandera
from domain.orm import Bandera, IndicadorFinanciero, Institucion

pytestmark = [pytest.mark.requires_docker, pytest.mark.usefixtures("catalogo_cargado")]


async def _id_de(nombre: str) -> int:
    async with session_scope() as session:
        institucion = await session.scalar(select(Institucion).where(Institucion.nombre == nombre))
    assert institucion is not None
    return institucion.id


async def test_requires_authentication(api: AsyncClient) -> None:
    assert (await api.get("/api/v1/instituciones/1")).status_code == 401


async def test_unknown_institution_returns_404(api_lectura: AsyncClient) -> None:
    respuesta = await api_lectura.get("/api/v1/instituciones/99999")
    assert respuesta.status_code == 404


async def test_a_nonexistent_reference_is_a_404_not_a_422(api_lectura: AsyncClient) -> None:
    """Desde que la ruta acepta slugs, `0` es una referencia con forma válida.

    Antes el path estaba tipado como `int(gt=0)` y el 0 se rechazaba en la
    validación. Ahora cualquier cadena es sintácticamente aceptable y lo único
    que se puede decir de una que no está en el catálogo es que no existe.
    """
    assert (await api_lectura.get("/api/v1/instituciones/0")).status_code == 404
    assert (await api_lectura.get("/api/v1/instituciones/no-existe")).status_code == 404


async def test_indicators_come_with_their_status(api_lectura: AsyncClient) -> None:
    """Las cuatro tarjetas del detalle salen de la API, no del frontend.

    Ahorra+ Capital tiene la salud en rango; su única bandera es la de GAT,
    que no es un indicador. Alcancía Fuerte los tiene en alerta.
    """
    sana = (await api_lectura.get("/api/v1/instituciones/ahorra-mas-capital")).json()
    estados = {i["clave"]: i["estado"] for i in sana["indicadores_ultimo_periodo"]["evaluados"]}

    assert estados["IMOR"] == "EN_RANGO"
    assert estados["NICAP"] == "EN_RANGO"
    assert estados["ICOR"] == "EN_RANGO"
    assert estados["ICAP"] == "SIN_DATO"  # las SOFIPOs reportan NICAP
    assert estados["CAPTACION"] == "INFORMATIVO"

    enferma = (await api_lectura.get("/api/v1/instituciones/alcancia-fuerte")).json()
    estados = {i["clave"]: i["estado"] for i in enferma["indicadores_ultimo_periodo"]["evaluados"]}

    assert estados["IMOR"] == "ALERTA"
    assert estados["NICAP"] == "ALERTA"
    assert estados["ICOR"] == "ALERTA"


async def test_institutions_without_cnbv_data_have_no_indicators(
    api_lectura: AsyncClient,
) -> None:
    """Sin boletines no hay tarjetas que pintar, y eso se dice explícitamente."""
    cuerpo = (await api_lectura.get("/api/v1/instituciones/finsus")).json()
    assert cuerpo["indicadores_ultimo_periodo"] is None


async def test_resolves_by_slug(api_lectura: AsyncClient) -> None:
    """La URL indexable del frontend es `/institucion/{slug}`."""
    respuesta = await api_lectura.get("/api/v1/instituciones/finsus")

    assert respuesta.status_code == 200
    assert respuesta.json()["nombre"] == "Finsus"


async def test_slug_and_id_return_the_same_institution(api_lectura: AsyncClient) -> None:
    por_id = (await api_lectura.get(f"/api/v1/instituciones/{await _id_de('Finsus')}")).json()
    por_slug = (await api_lectura.get("/api/v1/instituciones/finsus")).json()

    assert por_id == por_slug


async def test_returns_the_full_detail(api_lectura: AsyncClient) -> None:
    respuesta = await api_lectura.get(f"/api/v1/instituciones/{await _id_de('Finsus')}")

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["nombre"] == "Finsus"
    assert cuerpo["categoria"] == "SOFIPO"
    assert cuerpo["tipo_seguro"] == "PROSOFIPO"
    assert len(cuerpo["productos"]) == 4
    assert cuerpo["disclaimer"]


async def test_coverage_is_resolved_in_pesos(api_lectura: AsyncClient) -> None:
    cuerpo = (await api_lectura.get(f"/api/v1/instituciones/{await _id_de('Finsus')}")).json()

    cobertura = cuerpo["cobertura"]
    assert cobertura["limite_udis"] == "25000"
    assert Decimal(cobertura["limite_mxn"]) > Decimal("219000")
    assert cobertura["sin_limite"] is False
    assert cobertura["sin_cobertura"] is False


async def test_bank_coverage_beats_sofipo_coverage(api_lectura: AsyncClient) -> None:
    """Nu está clasificada como banco: su cobertura debe reflejarlo."""
    nu = (await api_lectura.get(f"/api/v1/instituciones/{await _id_de('Nu México')}")).json()
    finsus = (await api_lectura.get(f"/api/v1/instituciones/{await _id_de('Finsus')}")).json()

    assert nu["tipo_seguro"] == "IPAB"
    assert Decimal(nu["cobertura"]["limite_mxn"]) > Decimal(finsus["cobertura"]["limite_mxn"])


async def test_sovereign_debt_has_no_coverage_limit(api_lectura: AsyncClient) -> None:
    cuerpo = (
        await api_lectura.get(f"/api/v1/instituciones/{await _id_de('Gobierno Federal')}")
    ).json()

    assert cuerpo["cobertura"]["limite_mxn"] is None
    assert cuerpo["cobertura"]["sin_limite"] is True


async def test_ifpe_reports_zero_coverage(api_lectura: AsyncClient) -> None:
    """Distinto de sin límite: es lo contrario."""
    cuerpo = (
        await api_lectura.get(f"/api/v1/instituciones/{await _id_de('Mercado Pago')}")
    ).json()

    assert cuerpo["cobertura"]["limite_mxn"] == "0.00"
    assert cuerpo["cobertura"]["sin_cobertura"] is True
    assert cuerpo["cobertura"]["sin_limite"] is False


async def test_published_rates_carry_provenance(api_lectura: AsyncClient) -> None:
    """§19: ninguna tasa se muestra sin fecha ni fuente."""
    cuerpo = (
        await api_lectura.get(f"/api/v1/instituciones/{await _id_de('Gobierno Federal')}")
    ).json()

    con_tasa = [p for p in cuerpo["productos"] if p["tasa_nominal"] is not None]
    assert con_tasa
    for producto in con_tasa:
        assert producto["procedencia"]["fecha_dato"]
        assert producto["procedencia"]["fuente"]


async def test_unverified_rates_are_marked_as_such(api_lectura: AsyncClient) -> None:
    """Finsus sólo tiene tasas PENDIENTE_REVISION.

    Con el modo demo encendido salen, pero ninguna se presenta como
    confirmada. Que el detalle las muestre es deliberado: es la capa de
    profundidad de §11, donde el usuario que llega hasta aquí quiere ver todo
    lo que hay — siempre que se le diga qué es cada cosa.
    """
    cuerpo = (await api_lectura.get(f"/api/v1/instituciones/{await _id_de('Finsus')}")).json()

    con_tasa = [p for p in cuerpo["productos"] if p["procedencia"] is not None]
    assert con_tasa, "el seed trae tasas pendientes de Finsus"
    assert all(p["procedencia"]["verificada"] is False for p in con_tasa)
    assert all(p["procedencia"]["estado"] == "PENDIENTE_REVISION" for p in con_tasa)


async def test_products_without_a_rate_still_appear(api_lectura: AsyncClient) -> None:
    """El producto existe aunque no tenga tasa publicable todavía."""
    cuerpo = (await api_lectura.get(f"/api/v1/instituciones/{await _id_de('Finsus')}")).json()

    assert len(cuerpo["productos"]) == 4
    assert {p["plazo_dias"] for p in cuerpo["productos"]} == {28, 91, 182, 364}


async def test_cetes_get_a_computed_gat(api_lectura: AsyncClient) -> None:
    """§4.4: CETES no publica GAT, así que se calcula y se marca como tal."""
    cuerpo = (
        await api_lectura.get(f"/api/v1/instituciones/{await _id_de('Gobierno Federal')}")
    ).json()

    cete = next(p for p in cuerpo["productos"] if p["slug"] == "cetes-28")
    assert cete["gat"]["origen"] == "CALCULADA"
    assert cete["gat"]["es_calculada"] is True
    assert Decimal(cete["ten"]) < Decimal(cete["tasa_nominal"])


async def test_indicators_and_flags_are_empty_without_cnbv_data(
    api_lectura: AsyncClient,
) -> None:
    """No hay boletines cargados: no se inventan indicadores ni banderas."""
    cuerpo = (await api_lectura.get(f"/api/v1/instituciones/{await _id_de('Finsus')}")).json()

    assert cuerpo["indicadores_ultimo_periodo"] is None
    assert cuerpo["banderas_activas"] == []
    assert cuerpo["banderas_historicas"] == []


async def test_active_and_historical_flags_are_separated(
    api_lectura: AsyncClient,
) -> None:
    """Una institución que estuvo marcada cuenta una historia distinta."""
    institucion_id = await _id_de("Finsus")
    async with session_scope() as session:
        session.add_all(
            [
                Bandera(
                    institucion_id=institucion_id,
                    tipo=TipoBandera.IMOR,
                    severidad=Severidad.ROJA,
                    motivo="Morosidad del 9%",
                    periodo_dato=date(2026, 3, 31),
                    activa=True,
                ),
                Bandera(
                    institucion_id=institucion_id,
                    tipo=TipoBandera.ICAP,
                    severidad=Severidad.AMARILLA,
                    motivo="Capitalización ajustada",
                    periodo_dato=date(2025, 12, 31),
                    activa=False,
                ),
            ]
        )

    cuerpo = (await api_lectura.get(f"/api/v1/instituciones/{institucion_id}")).json()

    assert [b["tipo"] for b in cuerpo["banderas_activas"]] == ["IMOR"]
    assert [b["tipo"] for b in cuerpo["banderas_historicas"]] == ["ICAP"]
    assert cuerpo["banderas_activas"][0]["periodo_dato"] == "2026-03-31"


async def test_latest_period_indicators_are_returned(api_lectura: AsyncClient) -> None:
    institucion_id = await _id_de("Finsus")
    async with session_scope() as session:
        session.add_all(
            [
                IndicadorFinanciero(
                    institucion_id=institucion_id,
                    periodo=date(2025, 12, 31),
                    imor=Decimal("2.0"),
                ),
                IndicadorFinanciero(
                    institucion_id=institucion_id,
                    periodo=date(2026, 3, 31),
                    imor=Decimal("4.2"),
                    icap=Decimal("13.5"),
                    nicap_nivel=NivelCapitalizacion.N2,
                ),
            ]
        )

    cuerpo = (await api_lectura.get(f"/api/v1/instituciones/{institucion_id}")).json()
    indicadores = cuerpo["indicadores_ultimo_periodo"]

    assert indicadores["periodo"] == "2026-03-31"
    assert indicadores["imor"] == "4.2000"
    assert indicadores["nicap_nivel"] == "N2"
