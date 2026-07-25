"""Tests de las migraciones Alembic.

Verifican lo que el ORM por sí solo no garantiza: que la migración construye
el esquema desde una base vacía, que es reversible, y —lo más importante— que
no ha quedado deriva entre el modelo y la migración. Sin este último test,
alguien añade una columna al ORM, los tests de ORM siguen verdes porque usan
`create_all`, y el deploy se rompe.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from domain.orm import Base

pytestmark = pytest.mark.requires_docker

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Todas las tablas del esquema, más la bitácora de Alembic.
TABLAS_ESPERADAS = {
    "instituciones",
    "productos",
    "tasas",
    "indicadores_financieros",
    "banderas",
    "series_economicas",
    "valores_serie",
    "parametros_fiscales",
    "fuentes_tasas",
    "revisiones_tasas",
    "config_store",
    "config_versions",
    "job_runs",
}


@pytest.fixture
def sync_url() -> Iterator[str]:
    """Base vacía y desechable. URL síncrona: Alembic la abre él mismo."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16") as container:
        yield (
            f"postgresql+psycopg2://{container.username}:{container.password}"
            f"@{container.get_container_host_ip()}:{container.get_exposed_port(5432)}"
            f"/{container.dbname}"
        )


def _alembic_config(url: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    # env.py prefiere el -x url sobre settings.database_url.
    config.cmd_opts = type("Opts", (), {"x": [f"url={url.replace('psycopg2', 'asyncpg')}"]})()
    return config


def _tablas(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_builds_the_whole_schema_from_an_empty_database(sync_url: str) -> None:
    assert _tablas(sync_url) == set()

    command.upgrade(_alembic_config(sync_url), "head")

    tablas = _tablas(sync_url)
    assert TABLAS_ESPERADAS <= tablas
    assert "alembic_version" in tablas


def test_downgrade_is_reversible(sync_url: str) -> None:
    config = _alembic_config(sync_url)
    command.upgrade(config, "head")
    command.downgrade(config, "-1")

    # Sólo debe sobrevivir la bitácora de Alembic.
    assert _tablas(sync_url) == {"alembic_version"}


def test_upgrade_is_reapplicable_after_a_downgrade(sync_url: str) -> None:
    config = _alembic_config(sync_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    assert TABLAS_ESPERADAS <= _tablas(sync_url)


def test_no_drift_between_the_orm_and_the_migrations(sync_url: str) -> None:
    """El test que evita el fallo silencioso.

    Si alguien añade una columna al ORM y olvida la migración, los tests de
    ORM siguen verdes (usan `create_all`) y el deploy revienta. Aquí se
    compara el metadata contra la base migrada y se exige diferencia cero.
    """
    command.upgrade(_alembic_config(sync_url), "head")

    engine = create_engine(sync_url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            diferencias = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert diferencias == [], f"El ORM y las migraciones han divergido: {diferencias}"
