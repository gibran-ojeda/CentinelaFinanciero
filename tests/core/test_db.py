"""Tests del motor de base de datos.

No requieren Postgres levantado: verifican la construcción perezosa del engine
y que `check_connection` degrada a `False` en vez de propagar.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

import core.db as db_module
from core.db import check_connection, dispose_engine, get_engine, get_sessionmaker


@pytest.fixture(autouse=True)
async def _reset_engine() -> None:
    await dispose_engine()


def test_engine_is_created_lazily_and_cached() -> None:
    assert db_module._engine is None
    engine = get_engine()
    assert isinstance(engine, AsyncEngine)
    assert get_engine() is engine


def test_engine_uses_configured_pool_settings() -> None:
    engine = get_engine()
    assert engine.pool.size() == 10
    # pool_pre_ping no es introspectable en el pool; se comprueba en el dialecto.
    assert engine.pool._pre_ping is True


def test_sessionmaker_is_bound_to_the_engine() -> None:
    maker = get_sessionmaker()
    assert maker.kw["bind"] is get_engine()
    assert maker.kw["expire_on_commit"] is False
    assert get_sessionmaker() is maker


async def test_check_connection_returns_false_without_database() -> None:
    """El healthcheck reporta el fallo, no lo propaga."""
    assert await check_connection() is False


async def test_dispose_engine_clears_cached_objects() -> None:
    get_sessionmaker()
    await dispose_engine()
    assert db_module._engine is None
    assert db_module._sessionmaker is None
