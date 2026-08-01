"""Tests del descubrimiento contra la forma real del API de SharePoint.

Los cuerpos son los que devolvió `portafolioinfo.cnbv.gob.mx`, recortados: el
año como cadena en `A_x00f1_o`, el mes en español, el archivo colgando de
`File` porque se pidió con `$expand`, y la paginación por `odata.nextLink`.
"""

from __future__ import annotations

import ssl
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from ingest_cnbv import fuentes
from ingest_cnbv.downloader import (
    CERTIFICADO_INTERMEDIO,
    BoletinNoPublicado,
    DescargadorCNBV,
    ErrorCNBV,
    contexto_tls,
)

BASE = "https://portafolio.test"
LISTA = f"{BASE}/_api/web/lists/getByTitle('PortafolioInformacion')/items"


def _descargador(max_reintentos: int = 0) -> DescargadorCNBV:
    return DescargadorCNBV(
        base_url=BASE,
        timeout_s=5.0,
        max_reintentos=max_reintentos,
        espera_base_s=0.001,
        espera_tope_s=0.002,
        # Un cliente propio evita construir el contexto TLS en cada test; el
        # contexto tiene su propio test más abajo.
        cliente=httpx.AsyncClient(),
    )


def _elemento(nombre: str, anio: str, mes: str, *, sector: str, tema: str) -> dict[str, object]:
    return {
        "File": {
            "Name": nombre,
            "ServerRelativeUrl": f"/PortafolioInformacion/{nombre}",
            "Length": "2447264",
        },
        "Sector": sector,
        "Tema": tema,
        "SubTema": "Boletín",
        "A_x00f1_o": anio,
        "Mes": mes,
    }


# ─── Descubrimiento ───────────────────────────────────────────


@respx.mock
async def test_the_most_recent_publication_is_first() -> None:
    """La lista llega en orden de alta y el WAF prohíbe `$orderby`."""
    respx.get(url__startswith=LISTA).mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    _elemento(
                        "BE BM 202601.xlsx",
                        "2026",
                        "Enero",
                        sector=fuentes.SECTOR_BANCA,
                        tema=fuentes.TEMA_BOLETINES,
                    ),
                    _elemento(
                        "BE BM 202605.xlsx",
                        "2026",
                        "Mayo",
                        sector=fuentes.SECTOR_BANCA,
                        tema=fuentes.TEMA_BOLETINES,
                    ),
                    _elemento(
                        "BE BM 202512.xlsx",
                        "2025",
                        "Diciembre",
                        sector=fuentes.SECTOR_BANCA,
                        tema=fuentes.TEMA_BOLETINES,
                    ),
                ]
            },
        )
    )

    async with _descargador() as descargador:
        ultima = await descargador.ultimo(sector=fuentes.SECTOR_BANCA, tema=fuentes.TEMA_BOLETINES)

    assert ultima.archivo == "BE BM 202605.xlsx"
    assert (ultima.anio, ultima.mes) == (2026, 5)


@respx.mock
async def test_the_period_is_the_end_of_the_month() -> None:
    """El boletín dice «cifras al 31 de mayo», y eso es lo que se guarda."""
    respx.get(url__startswith=LISTA).mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    _elemento(
                        "BE BM 202605.xlsx",
                        "2026",
                        "Mayo",
                        sector=fuentes.SECTOR_BANCA,
                        tema=fuentes.TEMA_BOLETINES,
                    )
                ]
            },
        )
    )

    async with _descargador() as descargador:
        ultima = await descargador.ultimo(sector=fuentes.SECTOR_BANCA, tema=fuentes.TEMA_BOLETINES)

    assert ultima.periodo == date(2026, 5, 31)


@respx.mock
async def test_december_ends_on_the_thirty_first() -> None:
    respx.get(url__startswith=LISTA).mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    _elemento(
                        "BE BM 202512.xlsx",
                        "2025",
                        "Diciembre",
                        sector=fuentes.SECTOR_BANCA,
                        tema=fuentes.TEMA_BOLETINES,
                    )
                ]
            },
        )
    )

    async with _descargador() as descargador:
        ultima = await descargador.ultimo(sector=fuentes.SECTOR_BANCA, tema=fuentes.TEMA_BOLETINES)

    assert ultima.periodo == date(2025, 12, 31)


@respx.mock
async def test_pagination_follows_the_next_link() -> None:
    siguiente = f"{LISTA}?%24skiptoken=Paged%3dTRUE%26p_ID%3d106"
    ruta = respx.get(url__startswith=LISTA).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "odata.nextLink": siguiente,
                    "value": [
                        _elemento(
                            "BE BM 202601.xlsx",
                            "2026",
                            "Enero",
                            sector=fuentes.SECTOR_BANCA,
                            tema=fuentes.TEMA_BOLETINES,
                        )
                    ],
                },
            ),
            httpx.Response(
                200,
                json={
                    "value": [
                        _elemento(
                            "BE BM 202605.xlsx",
                            "2026",
                            "Mayo",
                            sector=fuentes.SECTOR_BANCA,
                            tema=fuentes.TEMA_BOLETINES,
                        )
                    ]
                },
            ),
        ]
    )

    async with _descargador() as descargador:
        todas = await descargador.publicaciones(
            sector=fuentes.SECTOR_BANCA, tema=fuentes.TEMA_BOLETINES
        )

    assert ruta.call_count == 2
    assert "skiptoken" in str(ruta.calls[1].request.url)
    # Las dos páginas juntas y ordenadas de lo nuevo a lo viejo.
    assert [p.archivo for p in todas] == ["BE BM 202605.xlsx", "BE BM 202601.xlsx"]


@respx.mock
async def test_the_query_avoids_the_parameters_the_waf_rejects() -> None:
    """`$select` y `$orderby` devuelven 403: se ordena en Python."""
    ruta = respx.get(url__startswith=LISTA).mock(
        return_value=httpx.Response(200, json={"value": []})
    )

    async with _descargador() as descargador:
        await descargador.publicaciones(sector=fuentes.SECTOR_BANCA, tema=fuentes.TEMA_BOLETINES)

    pedida = str(ruta.calls.last.request.url)
    assert "select" not in pedida.lower()
    assert "orderby" not in pedida.lower()
    assert "expand" in pedida.lower()


@respx.mock
async def test_entries_without_a_month_are_ignored() -> None:
    """Manuales y calendarios cuelgan del mismo tema y no son de un periodo."""
    respx.get(url__startswith=LISTA).mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    _elemento(
                        "Manual.pdf",
                        "2026",
                        "",
                        sector=fuentes.SECTOR_SOFIPO,
                        tema=fuentes.TEMA_NCYAT,
                    ),
                    _elemento(
                        "ICAP_SOFIPOS_202605.pdf",
                        "2026",
                        "Mayo",
                        sector=fuentes.SECTOR_SOFIPO,
                        tema=fuentes.TEMA_NCYAT,
                    ),
                ]
            },
        )
    )

    async with _descargador() as descargador:
        todas = await descargador.publicaciones(
            sector=fuentes.SECTOR_SOFIPO, tema=fuentes.TEMA_NCYAT
        )

    assert [p.archivo for p in todas] == ["ICAP_SOFIPOS_202605.pdf"]


@respx.mock
async def test_an_old_format_is_filtered_out_by_extension() -> None:
    """La serie mezcla `.xls`, `.xlsm` y `.xlsx`; el parser sólo lee OOXML."""
    respx.get(url__startswith=LISTA).mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    _elemento(
                        "BE BM 201005.xls",
                        "2026",
                        "Mayo",
                        sector=fuentes.SECTOR_BANCA,
                        tema=fuentes.TEMA_BOLETINES,
                    )
                ]
            },
        )
    )

    async with _descargador() as descargador:
        with pytest.raises(BoletinNoPublicado, match="xlsx"):
            await descargador.ultimo(
                sector=fuentes.SECTOR_BANCA,
                tema=fuentes.TEMA_BOLETINES,
                extension="xlsx",
            )


@respx.mock
async def test_nothing_published_is_not_a_crash() -> None:
    respx.get(url__startswith=LISTA).mock(return_value=httpx.Response(200, json={"value": []}))

    async with _descargador() as descargador:
        with pytest.raises(BoletinNoPublicado):
            await descargador.ultimo(sector=fuentes.SECTOR_SOFIPO, tema=fuentes.TEMA_BOLETINES)


# ─── Descarga ─────────────────────────────────────────────────


@respx.mock
async def test_a_filename_with_spaces_is_encoded(tmp_path: Path) -> None:
    """«BE BM 202605.xlsx» lleva espacios; los de SOFIPOs, guiones bajos."""
    respx.get(url__startswith=LISTA).mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    _elemento(
                        "BE BM 202605.xlsx",
                        "2026",
                        "Mayo",
                        sector=fuentes.SECTOR_BANCA,
                        tema=fuentes.TEMA_BOLETINES,
                    )
                ]
            },
        )
    )
    descarga = respx.get(
        "https://portafolioinfo.cnbv.gob.mx/PortafolioInformacion/BE%20BM%20202605.xlsx"
    ).mock(return_value=httpx.Response(200, content=b"contenido"))

    async with _descargador() as descargador:
        publicacion = await descargador.ultimo(
            sector=fuentes.SECTOR_BANCA, tema=fuentes.TEMA_BOLETINES
        )
        destino = await descargador.descargar(publicacion, tmp_path / "sub" / "b.xlsx")

    assert descarga.called
    assert destino.read_bytes() == b"contenido"


@respx.mock
async def test_a_403_explains_that_it_is_the_waf() -> None:
    """Un 403 pelado manda a buscar credenciales que este portal no pide."""
    respx.get(url__startswith=LISTA).mock(return_value=httpx.Response(403, text="Forbidden"))

    async with _descargador() as descargador:
        with pytest.raises(ErrorCNBV, match="WAF"):
            await descargador.publicaciones(
                sector=fuentes.SECTOR_BANCA, tema=fuentes.TEMA_BOLETINES
            )


@respx.mock
async def test_a_500_is_retried() -> None:
    ruta = respx.get(url__startswith=LISTA).mock(
        side_effect=[
            httpx.Response(500, text="boom"),
            httpx.Response(200, json={"value": []}),
        ]
    )

    async with _descargador(max_reintentos=1) as descargador:
        await descargador.publicaciones(sector=fuentes.SECTOR_BANCA, tema=fuentes.TEMA_BOLETINES)

    assert ruta.call_count == 2


# ─── TLS ──────────────────────────────────────────────────────


def test_the_missing_intermediate_ships_with_the_package() -> None:
    """La CNBV manda sólo la hoja; sin el intermedio, Linux no verifica.

    Se aporta el eslabón que falta en vez de apagar la verificación: se sigue
    exigiendo cadena completa hasta una raíz de confianza.
    """
    assert CERTIFICADO_INTERMEDIO.exists()
    texto = CERTIFICADO_INTERMEDIO.read_text(encoding="ascii")
    assert texto.startswith("-----BEGIN CERTIFICATE-----")

    contexto = contexto_tls()
    assert contexto.verify_mode is ssl.CERT_REQUIRED
    assert contexto.check_hostname is True
    emisores = {c["subject"][-1][0][1] for c in contexto.get_ca_certs()}  # type: ignore[index]
    assert "GlobalSign RSA OV SSL CA 2018" in emisores
