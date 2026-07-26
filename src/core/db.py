"""Motor async de SQLAlchemy y fábrica de sesiones.

El engine se crea de forma perezosa: importar este módulo no debe abrir
conexiones, porque los tests unitarios lo importan sin Postgres levantado.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.logging import get_logger
from core.settings import settings

log = get_logger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Devuelve el engine del proceso, creándolo en el primer uso."""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.db_echo,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            # pool_pre_ping descarta conexiones muertas antes de usarlas: el
            # scheduler mantiene conexiones ociosas horas y el Postgres del
            # VPS puede cortarlas por su lado.
            pool_pre_ping=True,
            pool_recycle=settings.db_pool_recycle_seconds,
        )
        log.debug(
            "db_engine_created",
            host=settings.postgres_host,
            port=settings.postgres_port,
            database=settings.postgres_db,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Sesión transaccional: commit al salir limpio, rollback ante excepción."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_connection() -> bool:
    """Ping a la base. `False` en vez de excepción — lo consume /healthz."""
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — el healthcheck reporta, no propaga
        log.warning("db_ping_failed", error=str(exc))
        return False
    return True


async def dispose_engine() -> None:
    """Cierra el pool. Se llama en el shutdown de la API y del scheduler."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        log.debug("db_engine_disposed")
    _engine = None
    _sessionmaker = None


__all__ = [
    "check_connection",
    "dispose_engine",
    "get_engine",
    "get_sessionmaker",
    "session_scope",
]
