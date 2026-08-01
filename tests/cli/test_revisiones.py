"""Tests de la cola de revisión por CLI.

Lo que importa aquí es que la CLI y los endpoints admin resuelvan **por el mismo
camino**: si cada uno implementara «aprobar» por su cuenta acabarían con dos
ideas de qué significa, y la que se usa a diario es ésta.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from cli import revisiones as cli_revisiones
from cli.seed import run_seed
from core.db import session_scope
from domain.enums import EstadoJob, EstadoRevision, EstadoTasa, FuenteTasa
from domain.orm import JobRun, Producto, RevisionTasa, Tasa

pytestmark = [pytest.mark.requires_docker, pytest.mark.usefixtures("real_db", "real_redis")]


async def _con_una_revision(motivo: str = "Primera lectura oficial de este producto") -> int:
    """Deja una tasa pendiente con su revisión y devuelve el id de la revisión."""
    await run_seed()
    async with session_scope() as session:
        producto = await session.scalar(
            select(Producto).where(Producto.slug == "finsus-plazo-364")
        )
        assert producto is not None
        tasa = Tasa(
            producto_id=producto.id,
            tasa_nominal=Decimal("8.69"),
            fecha_dato=date.today(),
            fuente=FuenteTasa.FETCH_DIRIGIDO,
            fuente_url="https://finsus.test/inversion",
            estado=EstadoTasa.PENDIENTE_REVISION,
        )
        session.add(tasa)
        await session.flush()
        revision = RevisionTasa(
            tasa_id=tasa.id,
            motivo=motivo,
            valor_anterior=Decimal("7.89"),
            valor_nuevo=Decimal("8.69"),
            estado=EstadoRevision.PENDIENTE,
        )
        session.add(revision)
        await session.flush()
        return int(revision.id)


async def test_the_queue_shows_what_is_needed_to_decide() -> None:
    """Institución, producto, de cuánto a cuánto, por qué y dónde comprobarlo."""
    await _con_una_revision()

    salida = await cli_revisiones.listar()

    assert "Finsus" in salida
    assert "7.89% → 8.69%" in salida
    assert "(+0.80 pp)" in salida
    assert "Primera lectura oficial" in salida
    assert "https://finsus.test/inversion" in salida


async def test_approving_publishes_the_rate() -> None:
    revision_id = await _con_una_revision()

    salida = await cli_revisiones.resolver(
        revision_id, aprobar=True, revisor="gibran", comentario=None
    )

    assert "APROBADA por gibran" in salida
    async with session_scope() as session:
        revision = await session.get(RevisionTasa, revision_id)
        assert revision is not None
        tasa = await session.get(Tasa, revision.tasa_id)
    assert revision.estado is EstadoRevision.APROBADA
    assert revision.revisor == "gibran"
    assert tasa is not None and tasa.estado is EstadoTasa.VIGENTE


async def test_rejecting_discards_it_without_deleting_anything() -> None:
    revision_id = await _con_una_revision()

    await cli_revisiones.resolver(
        revision_id, aprobar=False, revisor="gibran", comentario="la página cambió de formato"
    )

    async with session_scope() as session:
        revision = await session.get(RevisionTasa, revision_id)
        assert revision is not None
        tasa = await session.get(Tasa, revision.tasa_id)
    assert revision.estado is EstadoRevision.RECHAZADA
    assert "la página cambió de formato" in revision.motivo
    # La tasa sigue existiendo: la tabla es append-only y la decisión queda.
    assert tasa is not None and tasa.estado is EstadoTasa.RECHAZADA


async def test_resolving_twice_is_refused() -> None:
    """Resolver dos veces sobrescribiría quién decidió y cuándo."""
    revision_id = await _con_una_revision()
    await cli_revisiones.resolver(revision_id, aprobar=True, revisor="a", comentario=None)

    with pytest.raises(SystemExit, match="ya está APROBADA"):
        await cli_revisiones.resolver(revision_id, aprobar=False, revisor="b", comentario=None)


async def test_an_unknown_revision_fails_loudly() -> None:
    await run_seed()

    with pytest.raises(SystemExit, match="no existe la revisión"):
        await cli_revisiones.resolver(9999, aprobar=True, revisor="a", comentario=None)


async def test_an_empty_queue_says_so() -> None:
    await run_seed()

    assert "No hay revisiones" in await cli_revisiones.listar()


def _corrida_con_huecos(
    job_id: str, huecos: list[dict[str, object]], *, estado: EstadoJob = EstadoJob.EXITOSO
) -> JobRun:
    return JobRun(job_id=job_id, estado=estado, metricas={"huecos_catalogo": huecos})


def _hueco(institucion: str, plazo: int, tasa: str = "8.69") -> dict[str, object]:
    return {
        "institucion": institucion,
        "producto": f"Plazo {plazo} días",
        "plazo_dias": plazo,
        "tasa_nominal": tasa,
        "url": "https://ejemplo.test/inversion",
    }


async def test_catalogue_gaps_from_recent_runs_are_shown() -> None:
    """Un plazo que el catálogo no tiene no es una revisión, y se ve aparte."""
    await run_seed()
    async with session_scope() as session:
        session.add(
            _corrida_con_huecos("tasas_fetch_dirigido", [_hueco("Finsus", 360)])
        )

    salida = await cli_revisiones.listar()

    assert "Huecos de catálogo" in salida
    assert "seeds/productos.yaml" in salida
    assert "360d" in salida
    assert "8.69%" in salida


async def test_gaps_from_the_vps_and_the_laptop_runs_merge_and_dedupe() -> None:
    """Dos ids, fuentes distintas: ninguna corrida borra lo que vio la otra.

    Con un solo id, la pasada local con navegador pisaba los huecos del job
    del lunes — y al revés. El hueco repetido sale una sola vez.
    """
    await run_seed()
    async with session_scope() as session:
        session.add(_corrida_con_huecos("tasas_fetch_dirigido", [_hueco("Supertasas", 30)]))
        session.add(
            _corrida_con_huecos(
                "tasas_fetch_manual", [_hueco("Finsus", 360), _hueco("Supertasas", 30)]
            )
        )

    salida = await cli_revisiones.listar()

    assert "Finsus" in salida
    assert salida.count("Supertasas") == 1
    assert "360d" in salida and "30d" in salida


async def test_gaps_from_a_failed_run_still_count() -> None:
    """Los huecos de una corrida fallida son igual de reales.

    Con el filtro de sólo-EXITOSO, un fallo posterior de la corrida hacía
    invisibles los plazos que sí alcanzó a descubrir.
    """
    await run_seed()
    async with session_scope() as session:
        session.add(
            _corrida_con_huecos(
                "tasas_fetch_manual", [_hueco("Klar", 45)], estado=EstadoJob.FALLIDO
            )
        )

    salida = await cli_revisiones.listar()

    assert "Klar" in salida
    assert "45d" in salida
