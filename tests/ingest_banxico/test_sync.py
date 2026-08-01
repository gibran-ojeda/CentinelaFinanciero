"""Tests de la sincronización incremental.

Contra Postgres real: lo que hay que probar es la idempotencia, y ésa vive en
la clave única `uq_valor_serie_fecha` y en las consultas que la evitan. Un doble
de la sesión probaría el diccionario de Python, que no es donde está el riesgo.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from core.db import session_scope
from domain.orm import SerieEconomica, ValorSerieEconomica
from ingest_banxico import series as catalogo
from ingest_banxico.client import ErrorSIE, Observacion
from ingest_banxico.sync import sincronizar

pytestmark = pytest.mark.requires_docker

HOY = date(2026, 7, 31)


class ClienteFalso:
    """Devuelve lo que se le diga y anota qué rangos le pidieron."""

    def __init__(self, respuestas: dict[str, list[Observacion]] | None = None) -> None:
        self._respuestas = respuestas or {}
        self.peticiones: list[tuple[tuple[str, ...], date, date]] = []
        self.cerrado = False

    async def rango(
        self, claves: list[str], *, desde: date, hasta: date
    ) -> dict[str, list[Observacion]]:
        self.peticiones.append((tuple(claves), desde, hasta))
        return {clave: list(self._respuestas.get(clave, [])) for clave in claves}

    async def cerrar(self) -> None:
        self.cerrado = True


class ClienteQueRevienta(ClienteFalso):
    async def rango(
        self, claves: list[str], *, desde: date, hasta: date
    ) -> dict[str, list[Observacion]]:
        raise ErrorSIE("el SIE no está")


async def _contar(clave: str) -> int:
    async with session_scope() as session:
        return int(
            await session.scalar(
                select(func.count(ValorSerieEconomica.id))
                .join(SerieEconomica, SerieEconomica.id == ValorSerieEconomica.serie_id)
                .where(SerieEconomica.clave_banxico == clave)
            )
            or 0
        )


async def test_the_catalogue_series_are_created_on_the_first_run(real_db: None) -> None:
    cliente = ClienteFalso()

    reporte = await sincronizar(cliente=cliente, hoy=HOY)  # type: ignore[arg-type]

    assert reporte.series_creadas == len(catalogo.CATALOGO)
    async with session_scope() as session:
        claves = set((await session.execute(select(SerieEconomica.clave_banxico))).scalars().all())
    assert claves == set(catalogo.claves())


async def test_observations_are_stored(real_db: None) -> None:
    cliente = ClienteFalso(
        {
            catalogo.UDI.clave: [
                Observacion(date(2026, 7, 30), Decimal("8.79")),
                Observacion(date(2026, 7, 31), Decimal("8.80")),
            ]
        }
    )

    reporte = await sincronizar(cliente=cliente, hoy=HOY)  # type: ignore[arg-type]

    assert reporte.observaciones == 2
    assert await _contar(catalogo.UDI.clave) == 2


async def test_a_second_run_with_the_same_data_writes_nothing(real_db: None) -> None:
    """El criterio de aceptación de la fase: reejecutar no duplica."""
    datos = {
        catalogo.UDI.clave: [
            Observacion(date(2026, 7, 30), Decimal("8.79")),
            Observacion(date(2026, 7, 31), Decimal("8.80")),
        ]
    }

    await sincronizar(cliente=ClienteFalso(datos), hoy=HOY)  # type: ignore[arg-type]
    segunda = await sincronizar(cliente=ClienteFalso(datos), hoy=HOY)  # type: ignore[arg-type]

    assert segunda.observaciones == 0
    assert await _contar(catalogo.UDI.clave) == 2


async def test_a_repeated_date_inside_one_response_is_only_written_once(
    real_db: None,
) -> None:
    cliente = ClienteFalso(
        {
            catalogo.UDI.clave: [
                Observacion(date(2026, 7, 30), Decimal("8.79")),
                Observacion(date(2026, 7, 30), Decimal("8.79")),
            ]
        }
    )

    reporte = await sincronizar(cliente=cliente, hoy=HOY)  # type: ignore[arg-type]

    assert reporte.observaciones == 1


async def test_the_second_run_asks_from_the_last_stored_date(real_db: None) -> None:
    """Incremental de verdad: no se vuelven a pedir tres años cada mañana."""
    await sincronizar(  # type: ignore[arg-type]
        cliente=ClienteFalso(
            {catalogo.UDI.clave: [Observacion(date(2026, 7, 30), Decimal("8.79"))]}
        ),
        hoy=HOY,
    )

    segundo = ClienteFalso()
    await sincronizar(cliente=segundo, hoy=HOY)  # type: ignore[arg-type]

    inicios = {clave: desde for claves, desde, _ in segundo.peticiones for clave in claves}
    assert inicios[catalogo.UDI.clave] == date(2026, 7, 30)
    # Las que no trajeron nada siguen arrancando en la carga inicial.
    assert inicios[catalogo.INPC.clave] < date(2024, 1, 1)


async def test_the_requested_range_reaches_into_the_future(real_db: None) -> None:
    """La UDI se publica con diez días de adelanto: cortar en hoy la perdería."""
    cliente = ClienteFalso()

    await sincronizar(cliente=cliente, hoy=HOY)  # type: ignore[arg-type]

    assert all(hasta > HOY for _, _, hasta in cliente.peticiones)


async def test_series_starting_the_same_day_share_one_request(real_db: None) -> None:
    cliente = ClienteFalso()

    await sincronizar(cliente=cliente, hoy=HOY)  # type: ignore[arg-type]

    # Ninguna serie tiene datos todavía, así que todas arrancan igual.
    assert len(cliente.peticiones) == 1
    assert set(cliente.peticiones[0][0]) == set(catalogo.claves())


async def test_a_failing_batch_is_reported_and_does_not_raise(real_db: None) -> None:
    reporte = await sincronizar(cliente=ClienteQueRevienta(), hoy=HOY)  # type: ignore[arg-type]

    assert reporte.observaciones == 0
    assert reporte.errores and "el SIE no está" in reporte.errores[0]


async def test_forcing_a_start_date_overrides_what_is_stored(real_db: None) -> None:
    await sincronizar(  # type: ignore[arg-type]
        cliente=ClienteFalso(
            {catalogo.UDI.clave: [Observacion(date(2026, 7, 30), Decimal("8.79"))]}
        ),
        hoy=HOY,
    )

    cliente = ClienteFalso()
    await sincronizar(cliente=cliente, desde=date(2026, 1, 1), hoy=HOY)  # type: ignore[arg-type]

    assert {desde for _, desde, _ in cliente.peticiones} == {date(2026, 1, 1)}


async def test_an_existing_series_keeps_its_name(real_db: None) -> None:
    """El seed la llama distinto que el catálogo. El job no pisa lo que hay."""
    async with session_scope() as session:
        session.add(
            SerieEconomica(
                clave_banxico=catalogo.UDI.clave,
                nombre="Valor de la UDI",
                unidad="MXN por UDI",
            )
        )

    await sincronizar(cliente=ClienteFalso(), hoy=HOY)  # type: ignore[arg-type]

    async with session_scope() as session:
        nombre = await session.scalar(
            select(SerieEconomica.nombre).where(SerieEconomica.clave_banxico == catalogo.UDI.clave)
        )
    assert nombre == "Valor de la UDI"
