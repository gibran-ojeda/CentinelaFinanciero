"""Tests del cliente Redis.

El punto de estos tests es la **degradación**: con Redis caído, ninguna
operación debe propagar excepción. La app tiene que seguir sirviendo.
"""

from __future__ import annotations

import pytest

import core.redis as redis_module
from core.redis import close, delete, delete_pattern, eval_script, get, ping, set


@pytest.fixture(autouse=True)
async def _reset_client() -> None:
    await close()


def test_client_is_created_lazily_and_cached() -> None:
    assert redis_module._client is None
    client = redis_module.get_client()
    assert redis_module.get_client() is client


def test_client_decodes_responses() -> None:
    client = redis_module.get_client()
    assert client.connection_pool.connection_kwargs["decode_responses"] is True


async def test_ping_returns_false_when_unavailable() -> None:
    assert await ping() is False


async def test_get_degrades_to_cache_miss() -> None:
    assert await get("comparador:v1:plazo=28") is None


async def test_set_reports_failure_without_raising() -> None:
    assert await set("k", "v", ttl_seconds=60) is False


async def test_delete_returns_zero_when_unavailable() -> None:
    assert await delete("a", "b") == 0
    # Sin claves ni siquiera toca la red.
    assert await delete() == 0


async def test_delete_pattern_returns_zero_when_unavailable() -> None:
    assert await delete_pattern("comparador:*") == 0


async def test_eval_script_returns_none_when_unavailable() -> None:
    assert await eval_script("return 1", [], []) is None


async def test_close_is_idempotent() -> None:
    redis_module.get_client()
    await close()
    assert redis_module._client is None
    await close()
