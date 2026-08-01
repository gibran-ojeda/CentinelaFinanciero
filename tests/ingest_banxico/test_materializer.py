"""Tests del materializador de CETES.

Contra Postgres real por lo mismo que el sync: lo que se prueba es que
reejecutar no duplica, y eso vive en la clave única y en la consulta que la
esquiva, no en lógica de Python.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from core.db import session_scope
from domain.enums import EstadoTasa, FuenteTasa, TipoInstrumento
from domain.orm import Producto, SerieEconomica, Tasa, ValorSerieEconomica
from ingest_banxico import series as catalogo
from ingest_banxico.materializer import materializar

pytestmark = pytest.mark.requires_docker

HOY = date(2026, 7, 31)


async def _sembrar_subastas(clave: str, puntos: list[tuple[date, str]]) -> None:
    async with session_scope() as session:
        serie = await session.scalar(
            select(SerieEconomica).where(SerieEconomica.clave_banxico == clave)
        )
        if serie is None:
            serie = SerieEconomica(clave_banxico=clave, nombre=f"serie {clave}", unidad="% anual")
            session.add(serie)
            await session.flush()
        for fecha, valor in puntos:
            session.add(ValorSerieEconomica(serie_id=serie.id, fecha=fecha, valor=Decimal(valor)))


async def _tasas_de(plazo: int) -> list[Tasa]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(Tasa)
                    .join(Producto, Producto.id == Tasa.producto_id)
                    .where(
                        Producto.instrumento == TipoInstrumento.CETES,
                        Producto.plazo_dias == plazo,
                    )
                    .order_by(Tasa.fecha_dato)
                )
            )
            .scalars()
            .all()
        )


async def test_the_latest_auction_becomes_a_published_rate(catalogo_cargado: None) -> None:
    await _sembrar_subastas(
        catalogo.CETES_28.clave, [(date(2026, 7, 23), "6.18"), (date(2026, 7, 30), "6.20")]
    )

    reporte = await materializar(hoy=HOY)

    assert reporte.publicadas == 1
    tasa = (await _tasas_de(28))[-1]
    assert tasa.tasa_nominal == Decimal("6.2000")
    assert tasa.fecha_dato == date(2026, 7, 30)
    assert tasa.fuente is FuenteTasa.BANXICO_API
    # Fuente oficial: se publica sin pasar por revisión humana.
    assert tasa.estado is EstadoTasa.VIGENTE


async def test_the_published_rate_carries_an_openable_source_url(
    catalogo_cargado: None,
) -> None:
    await _sembrar_subastas(catalogo.CETES_28.clave, [(date(2026, 7, 30), "6.20")])

    await materializar(hoy=HOY)

    tasa = (await _tasas_de(28))[-1]
    assert tasa.fuente_url == catalogo.URL_PUBLICA_SUBASTA
    assert tasa.fuente_url is not None and tasa.fuente_url.startswith("https://")


async def test_running_twice_publishes_nothing_the_second_time(
    catalogo_cargado: None,
) -> None:
    """Criterio de aceptación: reejecutar el job no genera duplicados."""
    await _sembrar_subastas(catalogo.CETES_28.clave, [(date(2026, 7, 30), "6.20")])

    primera = await materializar(hoy=HOY)
    segunda = await materializar(hoy=HOY)

    assert (primera.publicadas, segunda.publicadas) == (1, 0)
    assert len([t for t in await _tasas_de(28) if t.fuente is FuenteTasa.BANXICO_API]) == 1


async def test_a_missed_week_is_recovered_on_the_next_run(catalogo_cargado: None) -> None:
    """Si el job no corre un lunes, la subasta perdida entra a la siguiente."""
    await _sembrar_subastas(catalogo.CETES_28.clave, [(date(2026, 7, 16), "6.20")])
    await materializar(hoy=date(2026, 7, 16))

    await _sembrar_subastas(
        catalogo.CETES_28.clave, [(date(2026, 7, 23), "6.18"), (date(2026, 7, 30), "6.21")]
    )
    reporte = await materializar(hoy=HOY)

    assert reporte.publicadas == 2
    fechas = [t.fecha_dato for t in await _tasas_de(28) if t.fuente is FuenteTasa.BANXICO_API]
    assert fechas == [date(2026, 7, 16), date(2026, 7, 23), date(2026, 7, 30)]


async def test_gaps_are_not_filled_with_the_previous_value(catalogo_cargado: None) -> None:
    """CETES 364 no se subasta cada semana. No se inventa lo que no se subastó."""
    await _sembrar_subastas(
        catalogo.CETES_364.clave, [(date(2026, 7, 9), "7.10"), (date(2026, 7, 23), "6.93")]
    )

    await materializar(hoy=HOY)

    fechas = [t.fecha_dato for t in await _tasas_de(364) if t.fuente is FuenteTasa.BANXICO_API]
    # Sólo la última en el arranque: nada inventado para el 16, que no la hubo.
    assert fechas == [date(2026, 7, 23)]


async def test_no_gat_is_invented(catalogo_cargado: None) -> None:
    await _sembrar_subastas(catalogo.CETES_28.clave, [(date(2026, 7, 30), "6.20")])

    await materializar(hoy=HOY)

    tasa = (await _tasas_de(28))[-1]
    assert tasa.gat_nominal is None
    assert tasa.gat_real is None


async def test_future_dated_observations_are_not_published(catalogo_cargado: None) -> None:
    await _sembrar_subastas(catalogo.CETES_28.clave, [(date(2026, 8, 15), "9.99")])

    reporte = await materializar(hoy=HOY)

    assert reporte.publicadas == 0
    assert catalogo.CETES_28.clave in reporte.sin_dato


async def test_a_series_without_a_product_is_reported_not_raised(real_db: None) -> None:
    """Sin catálogo cargado no hay productos CETES: se reporta el hueco."""
    await _sembrar_subastas(catalogo.CETES_28.clave, [(date(2026, 7, 30), "6.20")])

    reporte = await materializar(hoy=HOY)

    assert reporte.publicadas == 0
    assert reporte.productos == 0
    assert len(reporte.sin_producto) == len(catalogo.CETES_POR_PLAZO)
