"""Tests del cliente del SIE contra la forma real de sus respuestas.

Los cuerpos de este archivo **no son inventados**: son los que devolvió la API
de Banxico al explorarla, recortados. Importa que lo sean, porque las tres
rarezas que el cliente maneja —serie sin la clave `datos`, respuesta multi-serie
desordenada, token inválido como 400— son justo las que un fixture escrito de
memoria no tendría.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest
import respx

from ingest_banxico.client import (
    ClienteSIE,
    ErrorLimiteSIE,
    ErrorSIE,
    ErrorTokenSIE,
    Observacion,
)

BASE = "https://sie.test/v1"


def _cliente(max_reintentos: int = 2) -> ClienteSIE:
    return ClienteSIE(
        "token-de-prueba",
        base_url=BASE,
        timeout_s=5.0,
        max_reintentos=max_reintentos,
        # Las esperas reales son de segundos; aquí sólo hace falta que el
        # camino del reintento se recorra, no que tarde.
        espera_base_s=0.001,
        espera_tope_s=0.002,
    )


def _serie(clave: str, titulo: str, datos: list[dict[str, str]] | None) -> dict[str, object]:
    """Una serie tal y como la arma el SIE. `datos=None` omite la clave."""
    serie: dict[str, object] = {"idSerie": clave, "titulo": titulo}
    if datos is not None:
        serie["datos"] = datos
    return serie


def _cuerpo(*series: dict[str, object]) -> dict[str, object]:
    return {"bmx": {"series": list(series)}}


# ─── Lectura normal ───────────────────────────────────────────


@respx.mock
async def test_reads_the_latest_value_of_a_series() -> None:
    respx.get(f"{BASE}/series/SP68257/datos/oportuno").mock(
        return_value=httpx.Response(
            200,
            json=_cuerpo(
                _serie("SP68257", "Valor de UDIS", [{"fecha": "10/08/2026", "dato": "8.797743"}])
            ),
        )
    )

    async with _cliente() as cliente:
        series = await cliente.oportuno(["SP68257"])

    assert series["SP68257"] == [Observacion(fecha=date(2026, 8, 10), valor=Decimal("8.797743"))]


@respx.mock
async def test_the_token_travels_in_the_bmx_header() -> None:
    ruta = respx.get(f"{BASE}/series/SP1/datos/oportuno").mock(
        return_value=httpx.Response(200, json=_cuerpo(_serie("SP1", "INPC", [])))
    )

    async with _cliente() as cliente:
        await cliente.oportuno(["SP1"])

    assert ruta.calls.last.request.headers["Bmx-Token"] == "token-de-prueba"


@respx.mock
async def test_a_range_is_requested_in_iso_and_parsed_from_ddmmyyyy() -> None:
    """La asimetría de formatos del SIE, que es fácil de invertir sin darse cuenta."""
    ruta = respx.get(f"{BASE}/series/SF43936/datos/2026-07-01/2026-07-31").mock(
        return_value=httpx.Response(
            200,
            json=_cuerpo(
                _serie(
                    "SF43936",
                    "Cetes a 28 días",
                    [{"fecha": "02/07/2026", "dato": "6.30"}],
                )
            ),
        )
    )

    async with _cliente() as cliente:
        series = await cliente.rango(["SF43936"], desde=date(2026, 7, 1), hasta=date(2026, 7, 31))

    assert ruta.called
    assert series["SF43936"][0].fecha == date(2026, 7, 2)


# ─── Las tres rarezas ─────────────────────────────────────────


@respx.mock
async def test_a_series_without_data_omits_the_key_and_does_not_explode() -> None:
    """El SIE **no** manda `datos: []`: manda la serie sin esa clave.

    Es lo que contesta un rango en el que la serie no tuvo publicación —CETES
    364 no se subasta todas las semanas—, o sea, el caso sano.
    """
    respx.get(f"{BASE}/series/SF43945/datos/2026-08-01/2026-08-05").mock(
        return_value=httpx.Response(200, json=_cuerpo(_serie("SF43945", "Cetes a 364 días", None)))
    )

    async with _cliente() as cliente:
        series = await cliente.rango(["SF43945"], desde=date(2026, 8, 1), hasta=date(2026, 8, 5))

    assert series == {"SF43945": []}


@respx.mock
async def test_multi_series_responses_are_matched_by_id_not_by_position() -> None:
    """Se piden 936, 939, 942, 945 y el SIE contesta 936, 945, 942, 939.

    Es literalmente lo que devolvió. Casar por posición asignaría la tasa de
    364 días al producto de 91: un error silencioso y de los caros.
    """
    respx.get(f"{BASE}/series/SF43936,SF43939,SF43942,SF43945/datos/oportuno").mock(
        return_value=httpx.Response(
            200,
            json=_cuerpo(
                _serie("SF43936", "Cetes a 28 días", [{"fecha": "30/07/2026", "dato": "6.20"}]),
                _serie("SF43945", "Cetes a 364 días", [{"fecha": "23/07/2026", "dato": "6.93"}]),
                _serie("SF43942", "Cetes a 182 días", [{"fecha": "30/07/2026", "dato": "6.78"}]),
                _serie("SF43939", "Cetes a 91 días", [{"fecha": "30/07/2026", "dato": "6.50"}]),
            ),
        )
    )

    async with _cliente() as cliente:
        series = await cliente.oportuno(["SF43936", "SF43939", "SF43942", "SF43945"])

    assert series["SF43936"][0].valor == Decimal("6.20")
    assert series["SF43939"][0].valor == Decimal("6.50")
    assert series["SF43942"][0].valor == Decimal("6.78")
    assert series["SF43945"][0].valor == Decimal("6.93")


@respx.mock
async def test_an_invalid_token_arrives_as_400_and_is_not_retried() -> None:
    ruta = respx.get(f"{BASE}/series/SP68257/datos/oportuno").mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "url": "https://www.banxico.org.mx/SieAPIRest/service/v1/token",
                    "mensaje": "Token inválido",
                    "detalle": "El token enviado no es válido, favor de verificar.",
                }
            },
        )
    )

    async with _cliente() as cliente:
        with pytest.raises(ErrorTokenSIE, match="Token inválido"):
            await cliente.oportuno(["SP68257"])

    # Un token malo no mejora esperando: un solo intento.
    assert ruta.call_count == 1


# ─── Huecos y datos ilegibles ─────────────────────────────────


@respx.mock
async def test_gaps_marked_ne_are_skipped_not_zeroed() -> None:
    """`N/E` es «no existe», no un cero. Un cero mentiría en el promedio."""
    respx.get(f"{BASE}/series/SF43783/datos/oportuno").mock(
        return_value=httpx.Response(
            200,
            json=_cuerpo(
                _serie(
                    "SF43783",
                    "TIIE a 28 días",
                    [
                        {"fecha": "01/07/2026", "dato": "6.7559"},
                        {"fecha": "02/07/2026", "dato": "N/E"},
                        {"fecha": "03/07/2026", "dato": "6.7561"},
                    ],
                )
            ),
        )
    )

    async with _cliente() as cliente:
        series = await cliente.oportuno(["SF43783"])

    assert [o.fecha.day for o in series["SF43783"]] == [1, 3]


@respx.mock
async def test_thousands_separators_are_tolerated() -> None:
    respx.get(f"{BASE}/series/SP1/datos/oportuno").mock(
        return_value=httpx.Response(
            200,
            json=_cuerpo(_serie("SP1", "INPC", [{"fecha": "01/06/2026", "dato": "1,145.131"}])),
        )
    )

    async with _cliente() as cliente:
        series = await cliente.oportuno(["SP1"])

    assert series["SP1"][0].valor == Decimal("1145.131")


@respx.mock
async def test_a_series_the_sie_never_returned_shows_up_empty() -> None:
    """Quien llama compara contra lo que pidió, no contra lo que llegó."""
    respx.get(f"{BASE}/series/SP1,SP68257/datos/oportuno").mock(
        return_value=httpx.Response(
            200,
            json=_cuerpo(_serie("SP1", "INPC", [{"fecha": "01/06/2026", "dato": "145.131"}])),
        )
    )

    async with _cliente() as cliente:
        series = await cliente.oportuno(["SP1", "SP68257"])

    assert series["SP68257"] == []


# ─── Reintentos ───────────────────────────────────────────────


@respx.mock
async def test_a_429_is_retried_and_then_succeeds() -> None:
    ruta = respx.get(f"{BASE}/series/SP1/datos/oportuno").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "0"}, json={"error": {"mensaje": "ya"}}),
            httpx.Response(
                200,
                json=_cuerpo(_serie("SP1", "INPC", [{"fecha": "01/06/2026", "dato": "145.131"}])),
            ),
        ]
    )

    async with _cliente() as cliente:
        series = await cliente.oportuno(["SP1"])

    assert ruta.call_count == 2
    assert series["SP1"][0].valor == Decimal("145.131")


@respx.mock
async def test_a_429_that_never_clears_ends_as_a_rate_limit_error() -> None:
    respx.get(f"{BASE}/series/SP1/datos/oportuno").mock(
        return_value=httpx.Response(429, json={"error": {"mensaje": "límite por token"}})
    )

    async with _cliente(max_reintentos=1) as cliente:
        with pytest.raises(ErrorLimiteSIE):
            await cliente.oportuno(["SP1"])


@respx.mock
async def test_a_500_is_retried_but_a_404_is_not() -> None:
    quinientos = respx.get(f"{BASE}/series/SP1/datos/oportuno").mock(
        return_value=httpx.Response(500, text="boom")
    )
    async with _cliente(max_reintentos=1) as cliente:
        with pytest.raises(ErrorSIE):
            await cliente.oportuno(["SP1"])
    assert quinientos.call_count == 2

    respx.reset()
    cuatrocientos = respx.get(f"{BASE}/series/SP2/datos/oportuno").mock(
        return_value=httpx.Response(404, text="no existe")
    )
    async with _cliente(max_reintentos=1) as cliente:
        with pytest.raises(ErrorSIE):
            await cliente.oportuno(["SP2"])
    assert cuatrocientos.call_count == 1


@respx.mock
async def test_a_timeout_is_transient() -> None:
    ruta = respx.get(f"{BASE}/series/SP1/datos/oportuno").mock(
        side_effect=httpx.ReadTimeout("tarde")
    )

    async with _cliente(max_reintentos=1) as cliente:
        with pytest.raises(ErrorSIE, match="timeout"):
            await cliente.oportuno(["SP1"])

    assert ruta.call_count == 2


# ─── Lotes y token ausente ────────────────────────────────────


@respx.mock
async def test_requests_are_split_into_batches() -> None:
    from ingest_banxico import client as modulo

    claves = [f"SP{n}" for n in range(modulo.MAX_SERIES_POR_PETICION + 3)]
    ruta = respx.get(url__regex=rf"{BASE}/series/.+/datos/oportuno").mock(
        return_value=httpx.Response(200, json=_cuerpo())
    )

    async with _cliente() as cliente:
        series = await cliente.oportuno(claves)

    assert ruta.call_count == 2
    assert set(series) == set(claves)


async def test_without_a_token_nothing_is_requested() -> None:
    """Sin `BANXICO_TOKEN` no se llama al SIE. El job lo consulta antes de correr."""
    cliente = ClienteSIE("", base_url=BASE)
    assert cliente.hay_token is False
    with pytest.raises(ErrorTokenSIE, match="vacío"):
        await cliente.oportuno(["SP1"])


@respx.mock
async def test_a_body_that_is_not_the_sie_shape_fails_loudly() -> None:
    respx.get(f"{BASE}/series/SP1/datos/oportuno").mock(
        return_value=httpx.Response(200, json={"otra": "cosa"})
    )

    async with _cliente() as cliente:
        with pytest.raises(ErrorSIE, match="bmx"):
            await cliente.oportuno(["SP1"])
