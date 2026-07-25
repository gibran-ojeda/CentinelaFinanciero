"""Entorno de Alembic.

La URL de conexión se toma de `core.settings`, nunca de `alembic.ini`: así no
hay credenciales versionadas y las migraciones apuntan al mismo sitio que la
aplicación en cualquier entorno.

El `target_metadata` es el del ORM, así que `alembic revision --autogenerate`
compara contra el modelo real y no contra una lista mantenida a mano.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from core.settings import settings
from domain.orm import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Permite apuntar a otra base (por ejemplo, un contenedor de test) sin tocar
# el `.env`, que es lo que hacen los tests de migraciones.
url = context.get_x_argument(as_dictionary=True).get("url") or settings.database_url
config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))

target_metadata = Base.metadata


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Detecta cambios de tipo y de nullable en los autogenerate.
        compare_type=True,
        compare_server_default=True,
    )


def run_migrations_offline() -> None:
    """Genera el SQL sin conectarse. Útil para revisar un deploy antes."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
