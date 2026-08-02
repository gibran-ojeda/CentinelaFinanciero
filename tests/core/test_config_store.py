"""Tests del ConfigStore.

Lo que se está verificando: que un cambio de umbral en la base llega a
`effective` sin reiniciar el proceso, que sin override se cae limpiamente a
`Settings`, y que cada cambio deja historial — §19 exige poder reconstruir qué
umbral estaba vigente cuándo.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest

import core.config_store as cs
import core.db as db_module
from core.config_store import (
    CONFIG_REGISTRY,
    REGISTRY_BY_KEY,
    ConfigError,
    ConfigSnapshot,
    effective,
    history,
    load_snapshot,
    set_value,
)
from core.settings import settings
from domain.orm import Base


@pytest.fixture(autouse=True)
def _clean_snapshot() -> AsyncIterator[None]:
    cs.invalidate()
    yield
    cs.invalidate()


# ─── Registry (sin base) ──────────────────────────────────────


def test_every_registry_key_exists_in_settings() -> None:
    """Un spec que apunte a un atributo inexistente rompería el fallback."""
    for spec in CONFIG_REGISTRY:
        assert hasattr(settings, spec.settings_attr), spec.settings_attr


def test_registry_covers_the_flag_thresholds_of_the_plan() -> None:
    esperadas = {
        "umbral_imor_amarilla",
        "umbral_imor_roja",
        "umbral_icap_amarilla",
        "umbral_icap_roja",
        "umbral_cobertura_amarilla",
        "umbral_cobertura_roja",
        "umbral_gat_inconsistencia_pp",
    }
    assert esperadas <= set(REGISTRY_BY_KEY)


def test_the_demo_switch_is_hot_configurable() -> None:
    """Se apaga en producción sin desplegar (paso 9 de la fase 6)."""
    assert "mostrar_tasas_sin_verificar" in REGISTRY_BY_KEY
    assert REGISTRY_BY_KEY["mostrar_tasas_sin_verificar"].value_type == "bool"


def test_registry_groups_are_the_five_of_the_plan() -> None:
    assert {spec.grupo for spec in CONFIG_REGISTRY} == {
        "banderas",
        "fiscal",
        "llm",
        "revision",
        "scheduler",
    }


def test_the_research_knobs_are_hot_tunable() -> None:
    """La calibración mueve estas tres con lo observado, sin deploy.

    En producción el compose no pasa sus variables al contenedor, así que la
    llave caliente es la única palanca real que existe.
    """
    assert REGISTRY_BY_KEY["llm_cost_daily_limit_usd"].value_type == "float"
    assert REGISTRY_BY_KEY["research_max_rondas"].value_type == "int"
    assert REGISTRY_BY_KEY["research_motores"].value_type == "str"


def test_effective_falls_back_to_settings_without_overrides() -> None:
    assert effective.umbral_imor_roja == settings.umbral_imor_roja
    assert effective.umbral_icap_roja == Decimal("10.5")


def test_effective_delegates_unknown_attributes_to_settings() -> None:
    """`effective` debe poder sustituir a `settings` sin agujeros."""
    assert effective.postgres_port == settings.postgres_port
    assert effective.environment == settings.environment


def test_reading_an_override_does_not_need_the_database() -> None:
    """El proxy es síncrono: leer un umbral no puede bloquear un cálculo."""
    cs._snapshot = ConfigSnapshot(
        values={"umbral_imor_roja": Decimal("5.5")}, loaded_at=time.monotonic()
    )
    assert effective.umbral_imor_roja == Decimal("5.5")


def test_snapshot_reports_staleness() -> None:
    fresco = ConfigSnapshot(values={}, loaded_at=time.monotonic())
    assert fresco.is_stale(ttl_seconds=60) is False
    viejo = ConfigSnapshot(values={}, loaded_at=time.monotonic() - 120)
    assert viejo.is_stale(ttl_seconds=60) is True


@pytest.mark.parametrize(
    ("raw", "tipo", "esperado"),
    [
        ("42", "int", 42),
        ("3.5", "float", 3.5),
        ("10.5", "decimal", Decimal("10.5")),
        ("true", "bool", True),
        ("SI", "bool", True),
        ("0", "bool", False),
        ("texto", "str", "texto"),
    ],
)
def test_values_are_coerced_by_declared_type(raw: str, tipo: str, esperado: object) -> None:
    assert cs._coerce(raw, tipo) == esperado  # type: ignore[arg-type]


def test_invalid_value_raises_a_config_error() -> None:
    with pytest.raises(ConfigError):
        cs._coerce("no-es-un-numero", "decimal")


async def test_snapshot_load_degrades_without_database() -> None:
    """Sin base, la app arranca y sirve con los defaults."""
    snapshot = await load_snapshot()
    assert snapshot.values == {}


# ─── Contra Postgres real ─────────────────────────────────────


@pytest.mark.requires_docker
class TestConBaseReal:
    @pytest.fixture(autouse=True)
    async def _database(self) -> AsyncIterator[None]:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from testcontainers.postgres import PostgresContainer

        with PostgresContainer("postgres:16") as container:
            url = (
                f"postgresql+asyncpg://{container.username}:{container.password}"
                f"@{container.get_container_host_ip()}:{container.get_exposed_port(5432)}"
                f"/{container.dbname}"
            )
            engine = create_async_engine(url)
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            previous = (db_module._engine, db_module._sessionmaker)
            db_module._engine = engine
            db_module._sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
            try:
                yield
            finally:
                await engine.dispose()
                db_module._engine, db_module._sessionmaker = previous

    async def test_set_value_is_visible_through_effective(self) -> None:
        """El caso de uso completo: cambiar un umbral sin reiniciar."""
        assert effective.umbral_imor_roja == Decimal("6.0")

        await set_value("umbral_imor_roja", "5.5", actor="test", motivo="ajuste")
        await effective.refresh()

        assert effective.umbral_imor_roja == Decimal("5.5")

    async def test_override_is_typed_not_a_string(self) -> None:
        await set_value("cache_comparador_ttl_seconds", "120", actor="test")
        await effective.refresh()
        assert effective.cache_comparador_ttl_seconds == 120
        assert isinstance(effective.cache_comparador_ttl_seconds, int)

    async def test_kill_switch_can_be_flipped_hot(self) -> None:
        assert effective.banderas_recompute_enabled is True
        await set_value("banderas_recompute_enabled", "false", actor="test")
        await effective.refresh()
        assert effective.banderas_recompute_enabled is False

    async def test_keys_without_override_still_fall_back(self) -> None:
        await set_value("umbral_imor_roja", "5.5", actor="test")
        await effective.refresh()
        # Ésta no se tocó: sigue viniendo de Settings.
        assert effective.umbral_icap_roja == settings.umbral_icap_roja

    async def test_every_change_is_versioned(self) -> None:
        """§19: reconstruir qué umbral estaba vigente cuándo."""
        await set_value("umbral_imor_roja", "5.5", actor="gibran", motivo="primer ajuste")
        await set_value("umbral_imor_roja", "4.8", actor="gibran", motivo="segundo ajuste")

        versiones = await history("umbral_imor_roja")
        assert [v.version for v in versiones] == [2, 1]
        assert [v.value for v in versiones] == ["4.8", "5.5"]
        assert versiones[0].motivo == "segundo ajuste"
        assert versiones[0].updated_by == "gibran"

    async def test_unknown_key_is_rejected(self) -> None:
        with pytest.raises(ConfigError):
            await set_value("umbral_inventado", "1", actor="test")

    async def test_invalid_value_is_rejected_before_writing(self) -> None:
        with pytest.raises(ConfigError):
            await set_value("umbral_imor_roja", "muy alto", actor="test")
        await effective.refresh()
        assert effective.umbral_imor_roja == Decimal("6.0")

    async def test_as_dict_reports_the_origin_of_each_value(self) -> None:
        await set_value("umbral_imor_roja", "5.5", actor="test")
        await effective.refresh()

        vista = effective.as_dict()
        assert vista["umbral_imor_roja"]["origen"] == "config_store"
        assert vista["umbral_imor_roja"]["valor"] == Decimal("5.5")
        assert vista["umbral_icap_roja"]["origen"] == "settings"
        assert set(vista) == set(REGISTRY_BY_KEY)
