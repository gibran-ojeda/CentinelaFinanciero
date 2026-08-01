"""Tests del comando `python -m cli banxico sync`."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from cli.__main__ import build_parser
from ingest_banxico import series as catalogo
from ingest_banxico.client import Observacion


def test_the_command_is_wired_with_its_optional_start_date() -> None:
    args = build_parser().parse_args(["banxico", "sync", "--desde", "2026-01-01"])

    assert (args.comando, args.subcomando) == ("banxico", "sync")
    assert args.desde == date(2026, 1, 1)


def test_the_start_date_is_optional() -> None:
    assert build_parser().parse_args(["banxico", "sync"]).desde is None


def test_a_malformed_start_date_is_rejected_by_the_parser() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["banxico", "sync", "--desde", "01/01/2026"])


async def test_without_a_token_the_command_says_where_to_get_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El job se omite en silencio; la persona en la terminal merece el enlace."""
    from cli import banxico

    class SinToken:
        hay_token = False

        async def cerrar(self) -> None:
            return None

    monkeypatch.setattr(banxico, "ClienteSIE", lambda *a, **k: SinToken())

    with pytest.raises(RuntimeError, match="SieAPIRest"):
        await banxico.correr_sync()


@pytest.mark.requires_docker
async def test_the_command_runs_the_same_code_as_the_job(
    catalogo_cargado: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli import banxico

    class ClienteFalso:
        hay_token = True

        async def rango(
            self, claves: list[str], *, desde: date, hasta: date
        ) -> dict[str, list[Observacion]]:
            respuestas = {
                catalogo.CETES_28.clave: [Observacion(date(2026, 7, 30), Decimal("6.20"))]
            }
            return {clave: list(respuestas.get(clave, [])) for clave in claves}

        async def cerrar(self) -> None:
            return None

    monkeypatch.setattr(banxico, "ClienteSIE", lambda *a, **k: ClienteFalso())

    reporte = await banxico.correr_sync()

    assert reporte.series.observaciones == 1
    assert reporte.tasas.publicadas == 1
    assert reporte.hubo_errores is False
    # `render` es lo que ve quien corre el comando: que no reviente y que diga
    # las dos mitades.
    assert "series" in reporte.render() and "tasas de CETES" in reporte.render()
