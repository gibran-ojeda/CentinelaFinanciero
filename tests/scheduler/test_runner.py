"""Tests del runner y del registro declarativo de jobs."""

from __future__ import annotations

import pytest
from apscheduler.triggers.interval import IntervalTrigger

from scheduler.registry import JobSpec, build_registry, enabled_jobs
from scheduler.runner import JOB_DEFAULTS, _guarded, build_scheduler


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


async def test_guarded_job_is_skipped_when_lock_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin Redis el lock no se obtiene y el job no debe ejecutarse."""
    ejecutado = False

    async def marcar() -> None:
        nonlocal ejecutado
        ejecutado = True

    await _guarded(_spec(func=marcar))()
    assert ejecutado is False


async def test_guarded_job_runs_when_lock_is_acquired(monkeypatch: pytest.MonkeyPatch) -> None:
    ejecutado = False

    async def marcar() -> None:
        nonlocal ejecutado
        ejecutado = True

    monkeypatch.setattr("scheduler.runner.locks.acquire", _always_acquires)
    monkeypatch.setattr("scheduler.runner.locks.release", _always_releases)

    await _guarded(_spec(func=marcar))()
    assert ejecutado is True


async def test_guarded_job_swallows_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un job que revienta no puede tumbar el scheduler."""

    async def revienta() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("scheduler.runner.locks.acquire", _always_acquires)
    monkeypatch.setattr("scheduler.runner.locks.release", _always_releases)

    await _guarded(_spec(func=revienta))()  # no debe propagar


async def _always_acquires(name: str, *, ttl_seconds: int | None = None) -> str:
    return "token"


async def _always_releases(name: str, token: str) -> bool:
    return True
