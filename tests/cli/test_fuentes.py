"""Tests del diagnóstico y la reparación del catálogo de fuentes.

Lo que se verifica es que las dos averías del 2026-08-02 sean **visibles** y
**reparables sin deploy**: la fuente que falla y la que se descarga
perfectamente sin publicar nunca una tasa. La segunda no deja ni un error y era
la que nadie podía ver.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from cli import fuentes
from cli.seed import run_seed
from core.db import session_scope
from domain.orm import FuenteTasas

pytestmark = [pytest.mark.requires_docker, pytest.mark.usefixtures("real_db")]

KLAR = "https://www.klar.mx/inversion"


async def _klar() -> FuenteTasas:
    async with session_scope() as session:
        fuente = await session.scalar(select(FuenteTasas).where(FuenteTasas.url == KLAR))
        assert fuente is not None
        session.expunge(fuente)
        return fuente


async def _todas_sanas() -> None:
    """Deja el catálogo entero como si acabara de producir tasas.

    Incluye reactivar: el seed trae dos fuentes apagadas a propósito y este
    helper existe para partir de un catálogo sin nada que mirar.
    """
    async with session_scope() as session:
        for fuente in (await session.execute(select(FuenteTasas))).scalars():
            fuente.ultimo_exito_at = datetime.now(UTC)
            fuente.activa = True
            fuente.pausada_motivo = None


async def test_a_source_that_never_produced_a_rate_is_listed_as_broken() -> None:
    """El caso que no deja ningún error: la URL apunta a la portada.

    Klar, Stori, Ualá, Mercado Pago, kubo y Finsus se descargan sin problema y
    el extractor no encuentra una sola tasa. Sin `ultimo_exito_at` eran
    indistinguibles de una lectura buena que no se había movido.
    """
    await run_seed()
    await _todas_sanas()
    async with session_scope() as session:
        fuente = await session.scalar(select(FuenteTasas).where(FuenteTasas.url == KLAR))
        assert fuente is not None
        fuente.ultimo_exito_at = None

    salida = await fuentes.listar(solo_rotas=True)

    assert KLAR in salida
    assert "nunca" in salida
    assert "nunca han producido una tasa" in salida


async def test_the_seed_ships_two_sources_switched_off_on_purpose() -> None:
    """Openbank y Supertasas, y las dos por un motivo que no se arregla solo.

    Openbank contesta 403 al bot identificado en los dos transportes, y la
    doctrina es no imitar un navegador para esquivar un WAF: esa fuente pasa a
    lectura manual. Supertasas dejó de existir como marca — su dominio redirige
    a Crediclub— y apuntarla a la página de Crediclub le atribuiría tasas de
    otra institución.
    """
    await run_seed()

    async with session_scope() as session:
        apagadas = (
            (await session.execute(select(FuenteTasas).where(FuenteTasas.activa.is_(False))))
            .scalars()
            .all()
        )

    assert {f.url for f in apagadas} == {
        "https://www.openbank.mx/cuenta-debito-open-plus",
        "https://www.supertasas.com/",
    }
    # Y salen nombradas, que es lo que impide que se olviden.
    salida = await fuentes.listar(solo_rotas=True)
    assert "PAUSADA" in salida
    assert "Openbank" in salida


async def test_a_failing_source_shows_its_last_error() -> None:
    await run_seed()
    await _todas_sanas()
    async with session_scope() as session:
        fuente = await session.scalar(select(FuenteTasas).where(FuenteTasas.url == KLAR))
        assert fuente is not None
        fuente.fallos_consecutivos = 3
        fuente.ultimo_error = "no conecta: ConnectError"

    salida = await fuentes.listar(solo_rotas=True)

    assert "3 fallos" in salida
    assert "no conecta: ConnectError" in salida


async def test_a_healthy_catalogue_has_nothing_to_show() -> None:
    await run_seed()
    await _todas_sanas()

    assert "Ninguna fuente con problemas" in await fuentes.listar(solo_rotas=True)
    # Sin el filtro sí salen todas: `list` es también el inventario.
    assert KLAR in await fuentes.listar()


async def test_resuming_forgets_the_failures_that_caused_the_pause() -> None:
    """Si el contador sobreviviera, el primer fallo la volvería a apagar."""
    await run_seed()
    fuente = await _klar()
    await fuentes.pausar(fuente.id, motivo="el sitio está en mantenimiento")

    pausada = await _klar()
    assert pausada.activa is False
    assert pausada.pausada_motivo == "el sitio está en mantenimiento"

    async with session_scope() as session:
        suya = await session.get(FuenteTasas, fuente.id)
        assert suya is not None
        suya.fallos_consecutivos = 6
        suya.ultimo_error = "HTTP 500"

    await fuentes.reanudar(fuente.id)

    reanudada = await _klar()
    assert reanudada.activa is True
    assert reanudada.fallos_consecutivos == 0
    assert reanudada.ultimo_error is None
    assert reanudada.pausada_motivo is None


async def test_fixing_a_url_replaces_it_instead_of_adding_a_row() -> None:
    """La reparación que el YAML no puede hacer.

    Editar el YAML inserta otra fila y deja la muerta activa, porque la clave
    del upsert incluye la URL. Y el hash se olvida: el contenido de la página
    nueva no tiene nada que ver con el de la vieja, así que conservarlo haría
    que la primera corrida la saltara por «sin cambios».
    """
    await run_seed()
    fuente = await _klar()
    async with session_scope() as session:
        suya = await session.get(FuenteTasas, fuente.id)
        assert suya is not None
        suya.ultimo_hash = "a" * 64
        suya.fallos_consecutivos = 4
        suya.activa = False

    salida = await fuentes.cambiar_url(fuente.id, "https://www.klar.mx/inversiones")

    async with session_scope() as session:
        de_klar = (
            (
                await session.execute(
                    select(FuenteTasas).where(FuenteTasas.institucion_id == fuente.institucion_id)
                )
            )
            .scalars()
            .all()
        )
    assert [f.url for f in de_klar] == ["https://www.klar.mx/inversiones"]
    assert de_klar[0].ultimo_hash is None
    assert de_klar[0].fallos_consecutivos == 0
    assert de_klar[0].activa is True
    # Y dice en voz alta que esto no sustituye al repo.
    assert "seeds/fuentes_tasas.yaml" in salida


async def test_a_url_that_would_collide_is_refused() -> None:
    """Sin esto, el cambio reventaría contra `uq_fuente_url` a media sesión."""
    await run_seed()
    async with session_scope() as session:
        fuente = await session.scalar(select(FuenteTasas).where(FuenteTasas.url == KLAR))
        assert fuente is not None
        session.add(
            FuenteTasas(institucion_id=fuente.institucion_id, url="https://www.klar.mx/otra")
        )
        fuente_id = fuente.id

    with pytest.raises(SystemExit, match="ya tiene la fuente"):
        await fuentes.cambiar_url(fuente_id, "https://www.klar.mx/otra")


async def test_a_value_that_is_not_a_url_is_refused() -> None:
    await run_seed()
    fuente = await _klar()

    with pytest.raises(SystemExit, match="no parece una URL"):
        await fuentes.cambiar_url(fuente.id, "klar.mx/inversiones")


async def test_an_unknown_source_says_so_instead_of_failing_silently() -> None:
    await run_seed()

    with pytest.raises(SystemExit, match="no existe la fuente"):
        await fuentes.reanudar(999_999)
