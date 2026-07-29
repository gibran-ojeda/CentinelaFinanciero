"""Tests del techo de gasto diario.

Contra un Redis real: lo que importa es que `INCRBYFLOAT` sea atómico y que la
llave expire, y ninguna de las dos cosas se prueba contra un doble.

Los dos grupos van en clases porque necesitan fixtures incompatibles — un Redis
vivo y uno caído — y a nivel de módulo se pisarían.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from llm import cost_tracker

pytestmark = pytest.mark.requires_docker


@pytest.mark.usefixtures("real_redis")
class TestConRedis:
    async def test_nothing_spent_at_the_start(self) -> None:
        await cost_tracker.reiniciar()
        assert await cost_tracker.gastado_hoy() == 0.0
        assert await cost_tracker.disponible(1.0) is True

    async def test_spending_accumulates(self) -> None:
        await cost_tracker.reiniciar()
        await cost_tracker.registrar(0.004)
        await cost_tracker.registrar(0.006)

        assert await cost_tracker.gastado_hoy() == pytest.approx(0.01)

    async def test_the_ceiling_closes_the_tap(self) -> None:
        await cost_tracker.reiniciar()
        await cost_tracker.registrar(1.0)

        assert await cost_tracker.disponible(1.0) is False
        # Y sigue cerrado para un límite menor, no sólo para el exacto.
        assert await cost_tracker.disponible(0.5) is False

    async def test_concurrent_calls_do_not_lose_cost(self) -> None:
        """Dos extracciones en paralelo no se pisan el contador."""
        await cost_tracker.reiniciar()

        await asyncio.gather(*(cost_tracker.registrar(0.001) for _ in range(50)))

        assert await cost_tracker.gastado_hoy() == pytest.approx(0.05)

    async def test_the_key_expires_so_it_rotates_on_its_own(self) -> None:
        from core import redis

        await cost_tracker.reiniciar()
        await cost_tracker.registrar(0.01)

        ttl = await redis.get_client().ttl(cost_tracker.PREFIJO + date.today().isoformat())
        assert 0 < ttl <= cost_tracker.TTL_SEGUNDOS

    async def test_a_zero_limit_allows_nothing(self) -> None:
        await cost_tracker.reiniciar()
        assert await cost_tracker.disponible(0.0) is False


@pytest.mark.usefixtures("dead_redis")
class TestSinRedis:
    async def test_a_dead_redis_does_not_suspend_the_ingest(self) -> None:
        """El contador es una red de seguridad, no un requisito para operar.

        Detener la ingesta porque el contador esté caído sería peor que el
        riesgo que cubre: el gasto real de una corrida son centavos, y el fallo
        queda en los logs.
        """
        assert await cost_tracker.disponible(1.0) is True
        assert await cost_tracker.registrar(0.01) == 0.0
