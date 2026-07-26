"""Tests del runner y del registro declarativo de jobs."""

from __future__ import annotations

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from scheduler.registry import JobSpec, build_registry, enabled_jobs
from scheduler.runner import JOB_DEFAULTS, _derive_cooldown, _guarded, build_scheduler


async def _noop() -> None:
    return None


def _spec(job_id: str = "test-job", **kwargs: object) -> JobSpec:
    defaults: dict[str, object] = {
        "id": job_id,
        "func": _noop,
        "trigger": IntervalTrigger(seconds=60),
        "name": "Job de prueba",
    }
    defaults.update(kwargs)
    return JobSpec(**defaults)  # type: ignore[arg-type]


def test_job_defaults_prevent_overlap_and_pileup() -> None:
    assert JOB_DEFAULTS["max_instances"] == 1
    assert JOB_DEFAULTS["coalesce"] is True
    assert JOB_DEFAULTS["misfire_grace_time"] == 60


def test_registry_contains_heartbeat() -> None:
    ids = {job.id for job in build_registry()}
    assert "heartbeat" in ids


def test_cold_gate_filters_disabled_jobs() -> None:
    registry = (_spec("activo", enabled=True), _spec("apagado", enabled=False))
    assert {job.id for job in enabled_jobs(registry)} == {"activo"}


def test_lock_name_defaults_to_job_id() -> None:
    assert _spec("mi-job").lock == "mi-job"
    assert _spec("mi-job", lock_name="compartido").lock == "compartido"


def test_scheduler_registers_enabled_jobs() -> None:
    scheduler = build_scheduler()
    try:
        assert scheduler.get_job("heartbeat") is not None
    finally:
        scheduler.shutdown(wait=False) if scheduler.running else None


def test_cooldown_is_shorter_than_the_interval() -> None:
    """La llave de tick debe caducar antes del siguiente disparo."""
    assert _derive_cooldown(IntervalTrigger(seconds=60)) == 48
    assert _derive_cooldown(IntervalTrigger(seconds=15)) == 12
    # Nunca cero, aunque el intervalo sea de un segundo.
    assert _derive_cooldown(IntervalTrigger(seconds=1)) >= 1


def test_cron_triggers_use_a_fixed_cooldown_window() -> None:
    """Un cron no tiene espaciado fijo; basta cubrir la deriva entre réplicas."""
    assert _derive_cooldown(CronTrigger(hour=7)) == 60


@pytest.mark.usefixtures("dead_redis")
async def test_guarded_job_is_skipped_when_redis_is_unavailable() -> None:
    """Sin Redis no hay exclusión posible: el job no corre."""
    ejecutado = False

    async def marcar() -> None:
        nonlocal ejecutado
        ejecutado = True

    await _guarded(_spec(func=marcar))()
    assert ejecutado is False


async def test_guarded_job_runs_when_tick_and_lock_are_won(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ejecutado = False

    async def marcar() -> None:
        nonlocal ejecutado
        ejecutado = True

    _patch_exclusion(monkeypatch, tick_won=True)
    await _guarded(_spec(func=marcar))()
    assert ejecutado is True


async def test_guarded_job_is_skipped_when_another_replica_won_the_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El caso que el lock por sí solo no cubre: dos réplicas, un disparo."""
    ejecutado = False

    async def marcar() -> None:
        nonlocal ejecutado
        ejecutado = True

    _patch_exclusion(monkeypatch, tick_won=False)
    await _guarded(_spec(func=marcar))()
    assert ejecutado is False


async def test_tick_is_released_when_the_lock_is_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Una corrida anterior en vuelo no consume el disparo actual."""
    liberado = False

    async def _release_tick(name: str) -> bool:
        nonlocal liberado
        liberado = True
        return True

    async def _lock_busy(name: str, *, ttl_seconds: int | None = None) -> None:
        return None

    _patch_exclusion(monkeypatch, tick_won=True)
    monkeypatch.setattr("scheduler.runner.locks.acquire", _lock_busy)
    monkeypatch.setattr("scheduler.runner.locks.release_tick", _release_tick)

    await _guarded(_spec())()
    assert liberado is True


async def test_guarded_job_swallows_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un job que revienta no puede tumbar el scheduler."""

    async def revienta() -> None:
        raise RuntimeError("boom")

    _patch_exclusion(monkeypatch, tick_won=True)
    await _guarded(_spec(func=revienta))()  # no debe propagar


def _patch_exclusion(monkeypatch: pytest.MonkeyPatch, *, tick_won: bool) -> None:
    async def _claim_tick(name: str, *, cooldown_seconds: int) -> bool:
        return tick_won

    async def _acquire(name: str, *, ttl_seconds: int | None = None) -> str:
        return "token"

    async def _release(name: str, token: str) -> bool:
        return True

    monkeypatch.setattr("scheduler.runner.locks.claim_tick", _claim_tick)
    monkeypatch.setattr("scheduler.runner.locks.acquire", _acquire)
    monkeypatch.setattr("scheduler.runner.locks.release", _release)
