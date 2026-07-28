"""Tests del gate de deriva de esquema.

Se prueba contra una base migrada de verdad y no contra `create_all`: lo que
el gate afirma en producción es que la base **que dejaron las migraciones**
coincide con el ORM, y `create_all` construye el esquema desde el propio ORM,
con lo que nunca podría discrepar. Un test así siempre pasaría y no probaría
nada.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.schema_check import comprobar_esquema

pytestmark = pytest.mark.requires_docker

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
async def base_migrada() -> Iterator[None]:
    """Postgres desechable con `alembic upgrade head` ya corrido."""
    from testcontainers.postgres import PostgresContainer

    import core.db as db_module

    with PostgresContainer("postgres:16") as container:
        sync_url = (
            f"postgresql+psycopg2://{container.username}:{container.password}"
            f"@{container.get_container_host_ip()}:{container.get_exposed_port(5432)}"
            f"/{container.dbname}"
        )
        async_url = sync_url.replace("psycopg2", "asyncpg")

        config = Config(str(REPO_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        config.cmd_opts = type("Opts", (), {"x": [f"url={async_url}"]})()
        # En un hilo aparte: `alembic/env.py` maneja el motor async con
        # `asyncio.run`, y desde una fixture async ya hay un loop corriendo.
        await asyncio.to_thread(command.upgrade, config, "head")

        engine = create_async_engine(async_url)
        previo = (db_module._engine, db_module._sessionmaker)
        db_module._engine = engine
        db_module._sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            yield
        finally:
            await engine.dispose()
            db_module._engine, db_module._sessionmaker = previo


async def _sql(sentencia: str) -> None:
    from core.db import get_engine

    async with get_engine().begin() as conn:
        await conn.execute(text(sentencia))


@pytest.mark.usefixtures("base_migrada")
async def test_a_freshly_migrated_database_passes() -> None:
    reporte = await comprobar_esquema()

    assert reporte.ok
    assert reporte.migraciones_al_dia
    assert reporte.diferencias == []
    assert reporte.revision_bd == reporte.revision_head


@pytest.mark.usefixtures("base_migrada")
async def test_a_missing_column_fails_the_gate() -> None:
    """El caso real: alguien añade la columna al ORM y olvida la migración."""
    await _sql("ALTER TABLE instituciones DROP COLUMN notas")

    reporte = await comprobar_esquema()

    assert not reporte.ok
    # Sigue en el head: el número de revisión no basta para saber si la base
    # está bien, que es justamente por lo que este gate existe.
    assert reporte.migraciones_al_dia
    assert reporte.columnas_faltantes == ["instituciones.notas"]


@pytest.mark.usefixtures("base_migrada")
async def test_an_extra_column_fails_the_gate() -> None:
    await _sql("ALTER TABLE instituciones ADD COLUMN inventada TEXT")

    reporte = await comprobar_esquema()

    assert not reporte.ok
    assert reporte.columnas_sobrantes == ["instituciones.inventada"]


@pytest.mark.usefixtures("base_migrada")
async def test_a_nullability_mismatch_fails_the_gate() -> None:
    """Un NOT NULL que la base no tiene deja entrar filas que el ORM da por completas."""
    await _sql("ALTER TABLE instituciones ALTER COLUMN slug DROP NOT NULL")

    reporte = await comprobar_esquema()

    assert not reporte.ok
    assert len(reporte.nulabilidad) == 1
    assert "instituciones.slug" in reporte.nulabilidad[0]
    assert "NOT NULL" in reporte.nulabilidad[0]


@pytest.mark.usefixtures("base_migrada")
async def test_a_missing_table_fails_the_gate() -> None:
    await _sql("DROP TABLE banderas CASCADE")

    reporte = await comprobar_esquema()

    assert not reporte.ok
    assert reporte.tablas_faltantes == ["banderas"]


@pytest.mark.usefixtures("base_migrada")
async def test_an_unapplied_migration_fails_the_gate() -> None:
    """El esquema puede estar bien y la bitácora atrasada. También aborta."""
    await _sql("UPDATE alembic_version SET version_num = 'una-revision-vieja'")

    reporte = await comprobar_esquema()

    assert not reporte.ok
    assert not reporte.migraciones_al_dia
    assert reporte.diferencias == []
    assert "migraciones sin aplicar" in reporte.render()


@pytest.mark.usefixtures("base_migrada")
async def test_alembic_version_is_not_reported_as_an_extra_table() -> None:
    """Alembic la mantiene ella; no está en el metadata y no es deriva."""
    reporte = await comprobar_esquema()

    assert reporte.tablas_sobrantes == []
