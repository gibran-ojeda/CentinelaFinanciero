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

#: Las tablas del esquema inicial (`4dc69f00aae0`). Un rollback de UNA
#: revisión puede llevarse la tabla que esa migración creó, pero jamás estas:
#: son la vara del test de reversibilidad, que no puede usar el set completo
#: sin nombrar qué hace la última migración.
TABLAS_INICIALES = {
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

#: Todas las tablas del esquema en head: las iniciales más las que añadieron
#: migraciones posteriores. Toda tabla nueva se registra aquí.
TABLAS_ESPERADAS = TABLAS_INICIALES | {"tramos_tasas"}


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


def _foto_del_esquema(url: str) -> dict[str, object]:
    """Tablas, columnas, nulabilidad y restricciones CHECK.

    Los CHECK entran porque hay migraciones que no tocan ninguna columna: la
    que sincronizó `ck_fuente_valido` con `FuenteTasa` sólo cambia una lista de
    valores, y una foto que mirase únicamente las columnas la daría por
    inexistente — con lo que su reversibilidad no quedaría comprobada.
    """
    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        return {
            tabla: (
                sorted((c["name"], bool(c["nullable"])) for c in inspector.get_columns(tabla)),
                sorted(
                    (c["name"] or "", " ".join(str(c["sqltext"]).split()))
                    for c in inspector.get_check_constraints(tabla)
                ),
            )
            for tabla in sorted(set(inspector.get_table_names()) - {"alembic_version"})
        }
    finally:
        engine.dispose()


def test_upgrade_builds_the_whole_schema_from_an_empty_database(sync_url: str) -> None:
    assert _tablas(sync_url) == set()

    command.upgrade(_alembic_config(sync_url), "head")

    tablas = _tablas(sync_url)
    assert TABLAS_ESPERADAS <= tablas
    assert "alembic_version" in tablas


def test_downgrade_to_base_leaves_nothing_behind(sync_url: str) -> None:
    config = _alembic_config(sync_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    # Sólo debe sobrevivir la bitácora de Alembic.
    assert _tablas(sync_url) == {"alembic_version"}


def test_the_last_migration_is_reversible_on_its_own(sync_url: str) -> None:
    """Un paso atrás deshace **ese** paso, y volver a darlo lo restaura.

    Es el escenario de un rollback en producción: se despliega una revisión,
    algo falla y se retrocede una sola — el `alembic downgrade -1` del runbook.

    Se comprueba con una foto del esquema antes y después del viaje de ida y
    vuelta, en vez de nombrar lo que hace la última migración. Nombrarlo
    obligaba a reescribir este test con cada migración nueva, y un test que hay
    que reescribir para que vuelva a pasar deja de comprobar nada: se convierte
    en un trámite. Esta versión no hay que tocarla nunca más.
    """
    config = _alembic_config(sync_url)
    command.upgrade(config, "head")
    antes = _foto_del_esquema(sync_url)

    command.downgrade(config, "-1")
    # Un paso atrás no es un `downgrade base`: el resto del esquema sigue en
    # pie. La vara son las tablas iniciales — la última migración puede haber
    # creado una tabla y su rollback legítimamente se la lleva.
    assert TABLAS_INICIALES <= _tablas(sync_url)

    command.upgrade(config, "head")
    assert _foto_del_esquema(sync_url) == antes


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
