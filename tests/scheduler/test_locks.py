"""Tests del lock distribuido.

La exclusión mutua se prueba contra un Redis real (testcontainers): un doble en
memoria no ejercitaría la atomicidad de `SET NX` ni la del script Lua, que es
justamente lo que se está verificando.
"""

from __future__ import annotations

import asyncio

import pytest

from scheduler import locks


@pytest.mark.usefixtures("dead_redis")
async def test_acquire_returns_none_without_redis() -> None:
    """Sin Redis no hay garantía de exclusión: el job se salta la corrida."""
    assert await locks.acquire("heartbeat") is None


@pytest.mark.usefixtures("dead_redis")
async def test_job_lock_yields_false_without_redis() -> None:
    async with locks.job_lock("heartbeat") as adquirido:
        assert adquirido is False


@pytest.mark.usefixtures("dead_redis")
async def test_claim_tick_returns_false_without_redis() -> None:
    assert await locks.claim_tick("heartbeat", cooldown_seconds=30) is False


def test_lock_key_is_namespaced() -> None:
    assert locks.lock_key("heartbeat") == "centinela:lock:heartbeat"


@pytest.mark.requires_docker
@pytest.mark.usefixtures("real_redis")
class TestConRedisReal:
    async def test_second_acquire_is_rejected(self) -> None:
        primero = await locks.acquire("job-a", ttl_seconds=30)
        segundo = await locks.acquire("job-a", ttl_seconds=30)
        assert primero is not None
        assert segundo is None

    async def test_release_frees_the_lock(self) -> None:
        token = await locks.acquire("job-b", ttl_seconds=30)
        assert token is not None
        assert await locks.release("job-b", token) is True
        assert await locks.acquire("job-b", ttl_seconds=30) is not None

    async def test_release_with_foreign_token_is_a_noop(self) -> None:
        """El caso que motiva el compare-and-delete.

        Si el job A se pasa del TTL, el lock expira, B lo toma, y luego A
        termina y llama a release: no debe borrar el lock de B.
        """
        token_a = await locks.acquire("job-c", ttl_seconds=30)
        assert token_a is not None

        assert await locks.release("job-c", "token-de-otro") is False
        # El lock sigue tomado por A.
        assert await locks.acquire("job-c", ttl_seconds=30) is None
        assert await locks.release("job-c", token_a) is True

    async def test_lock_expires_with_its_ttl(self) -> None:
        assert await locks.acquire("job-d", ttl_seconds=1) is not None
        # 2 s y no 1.3: el margen sobre el TTL es para que **este** proceso
        # llegue a preguntar, no para que Redis expire la llave. Con la suite
        # completa en marcha —dos contenedores y un event loop ocupado— 300 ms
        # no siempre bastaban, y el test caía una vez cada muchas. Si el lock
        # de verdad no caducara, esperar más no lo salvaría.
        await asyncio.sleep(2.0)
        assert await locks.acquire("job-d", ttl_seconds=30) is not None

    async def test_extend_renews_only_for_the_owner(self) -> None:
        token = await locks.acquire("job-e", ttl_seconds=1)
        assert token is not None
        assert await locks.extend("job-e", "token-de-otro", ttl_seconds=60) is False
        assert await locks.extend("job-e", token, ttl_seconds=60) is True
        await asyncio.sleep(1.3)
        # Sigue tomado gracias a la renovación.
        assert await locks.acquire("job-e", ttl_seconds=30) is None

    async def test_only_one_of_many_concurrent_workers_wins(self) -> None:
        """Esto es lo que impide que dos réplicas del scheduler dupliquen un job."""
        resultados = await asyncio.gather(
            *(locks.acquire("job-f", ttl_seconds=30) for _ in range(10))
        )
        ganadores = [t for t in resultados if t is not None]
        assert len(ganadores) == 1

    async def test_job_lock_releases_even_when_the_job_raises(self) -> None:
        with pytest.raises(RuntimeError):
            async with locks.job_lock("job-g", ttl_seconds=30) as adquirido:
                assert adquirido is True
                raise RuntimeError("el job falló")

        # El lock quedó libre pese a la excepción.
        assert await locks.acquire("job-g", ttl_seconds=30) is not None

    async def test_only_one_replica_claims_a_tick(self) -> None:
        """El caso que el lock por sí solo no cubre.

        Un job corto toma y suelta el lock en un milisegundo, así que la
        réplica que dispara un instante después lo encuentra libre. El tick sí
        lo impide porque no se libera al terminar.
        """
        primera = await locks.claim_tick("job-h", cooldown_seconds=30)
        segunda = await locks.claim_tick("job-h", cooldown_seconds=30)
        assert primera is True
        assert segunda is False

    async def test_tick_expires_so_the_next_run_can_proceed(self) -> None:
        assert await locks.claim_tick("job-i", cooldown_seconds=1) is True
        # Mismo margen que el TTL del lock, y por lo mismo: esperar a llegar a
        # preguntar, no a que Redis expire.
        await asyncio.sleep(2.0)
        assert await locks.claim_tick("job-i", cooldown_seconds=1) is True

    async def test_release_tick_allows_an_immediate_retry(self) -> None:
        assert await locks.claim_tick("job-j", cooldown_seconds=300) is True
        assert await locks.release_tick("job-j") is True
        assert await locks.claim_tick("job-j", cooldown_seconds=300) is True

    async def test_tick_and_lock_use_separate_namespaces(self) -> None:
        """Tomar el tick no debe consumir el lock ni al revés."""
        assert await locks.claim_tick("job-k", cooldown_seconds=30) is True
        assert await locks.acquire("job-k", ttl_seconds=30) is not None

    async def test_concurrent_replicas_produce_exactly_one_run(self) -> None:
        """Reproduce el escenario de `docker compose --scale scheduler=2`."""
        corridas = 0

        async def replica() -> None:
            nonlocal corridas
            if not await locks.claim_tick("job-l", cooldown_seconds=30):
                return
            async with locks.job_lock("job-l", ttl_seconds=30) as adquirido:
                if adquirido:
                    corridas += 1

        # Secuencial a propósito: es el caso que fallaba, no el concurrente.
        await replica()
        await replica()
        assert corridas == 1
