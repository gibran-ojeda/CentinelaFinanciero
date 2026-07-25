"""Capa 2 de configuración: parámetros de negocio ajustables sin deploy.

`Settings` (capa 1) cubre infraestructura y secretos: cambiarlos exige
reiniciar. Los umbrales de banderas, las tolerancias de revisión y los TTLs
son distintos — hay que poder moverlos con el sistema en vivo, y hay que poder
auditar qué valor estaba activo cuándo (§19: "es reconstruible qué umbral
estaba vigente cuándo y qué bandera generó").

Piezas:

- `ConfigKeySpec` — registry declarativo de qué parámetros viven en BD. Cada
  spec apunta a un atributo de `Settings`, que es el valor por defecto: si no
  hay override en BD, se lee de ahí. Añadir un parámetro es añadir un spec.
- `ConfigSnapshot` — foto inmutable de los overrides, con TTL. Se refresca en
  bloque, nunca por clave: así una lectura no puede ver la mitad de un cambio.
- `effective` — proxy **síncrono** con los mismos nombres de atributo que
  `Settings`. Migrar un consumidor es cambiar `settings.x` por `effective.x`.

El proxy es síncrono a propósito: `metrics/` y los routers leen umbrales en
medio de cálculos, y obligarlos a `await` propagaría async por todo el motor
de métricas, que debe quedar puro.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from sqlalchemy import select

from core.db import session_scope
from core.logging import get_logger
from core.settings import settings
from domain.orm import ConfigStoreEntry, ConfigVersion

log = get_logger(__name__)

ValueType = Literal["int", "float", "decimal", "bool", "str"]

DEFAULT_TTL_SECONDS = 60


class ConfigError(Exception):
    """Clave desconocida o valor no convertible."""


@dataclass(frozen=True, slots=True)
class ConfigKeySpec:
    """Declara que un parámetro puede sobrescribirse desde la base."""

    settings_attr: str
    value_type: ValueType
    grupo: str
    description: str

    @property
    def key(self) -> str:
        return self.settings_attr


def _coerce(raw: str, value_type: ValueType) -> Any:
    try:
        match value_type:
            case "int":
                return int(raw)
            case "float":
                return float(raw)
            case "decimal":
                return Decimal(raw)
            case "bool":
                return raw.strip().lower() in {"1", "true", "yes", "on", "si", "sí"}
            case _:
                return raw
    except (ValueError, InvalidOperation) as exc:
        raise ConfigError(f"'{raw}' no es un {value_type} válido") from exc


def _serialize(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# ─── Registry ─────────────────────────────────────────────────

#: Qué parámetros son ajustables en caliente. El orden es documentación.
CONFIG_REGISTRY: tuple[ConfigKeySpec, ...] = (
    # Umbrales de banderas individuales (§5.1). Son los números que deciden
    # si una institución aparece marcada en el comparador, así que tienen que
    # poder moverse sin desplegar y con historial auditable.
    ConfigKeySpec(
        "umbral_imor_amarilla", "decimal", "banderas", "IMOR desde el que se marca 🟡 (%)"
    ),
    ConfigKeySpec("umbral_imor_roja", "decimal", "banderas", "IMOR desde el que se marca 🔴 (%)"),
    ConfigKeySpec(
        "umbral_icap_amarilla", "decimal", "banderas", "ICAP por debajo del cual va 🟡 (%)"
    ),
    ConfigKeySpec(
        "umbral_icap_roja",
        "decimal",
        "banderas",
        "ICAP por debajo del cual va 🔴 (%). Mínimo regulatorio SOFIPO: 10.5",
    ),
    ConfigKeySpec(
        "umbral_cobertura_amarilla",
        "decimal",
        "banderas",
        "Cobertura de cartera vencida por debajo de la cual va 🟡 (%)",
    ),
    ConfigKeySpec(
        "umbral_cobertura_roja",
        "decimal",
        "banderas",
        "Cobertura de cartera vencida por debajo de la cual va 🔴 (%)",
    ),
    ConfigKeySpec(
        "umbral_gat_inconsistencia_pp",
        "decimal",
        "banderas",
        "Diferencia GAT vs. tasa nominal que dispara 🟡 (puntos porcentuales)",
    ),
    ConfigKeySpec(
        "umbral_crecimiento_captacion_pct",
        "decimal",
        "banderas",
        "Crecimiento de captación considerado agresivo para la compuesta 🔴 (%)",
    ),
    ConfigKeySpec(
        "umbral_tasa_sobre_mercado_pp",
        "decimal",
        "banderas",
        "Puntos sobre la mediana del mercado que hacen sospechosa una tasa",
    ),
    ConfigKeySpec(
        "umbral_apalancamiento_amarilla",
        "decimal",
        "banderas",
        "Pasivo/capital por encima del cual va 🟡",
    ),
    # Fiscal e inflación. Valores de respaldo mientras las series de Banxico
    # no se pueblan automáticamente (eso llega en la fase 7): sin esto, ni la
    # cobertura en MXN ni la calculadora tienen de dónde salir.
    ConfigKeySpec(
        "udi_valor_fallback",
        "decimal",
        "fiscal",
        "Valor UDI a usar si la serie de Banxico no tiene datos",
    ),
    ConfigKeySpec(
        "inflacion_anual_fallback",
        "decimal",
        "fiscal",
        "Inflación INPC anual a usar si la serie no tiene datos (%)",
    ),
    # Revisión de tasas de origen LLM (fase 9).
    ConfigKeySpec(
        "tolerancia_revision_pp",
        "decimal",
        "revision",
        "Desviación respecto a la tasa vigente que manda a revisión humana (pp)",
    ),
    ConfigKeySpec(
        "mostrar_datos_demo",
        "bool",
        "revision",
        (
            "Publica instituciones ilustrativas y tasas sin verificar, marcadas. "
            "Apagar antes de exponer el sitio a internet (fase 6)"
        ),
    ),
    # Operación.
    ConfigKeySpec(
        "cache_comparador_ttl_seconds", "int", "scheduler", "TTL del cache del comparador"
    ),
    ConfigKeySpec(
        "banderas_recompute_enabled",
        "bool",
        "scheduler",
        "Kill-switch caliente del recomputo de banderas",
    ),
    ConfigKeySpec("config_cache_ttl_seconds", "int", "scheduler", "TTL del snapshot de config"),
)

REGISTRY_BY_KEY: dict[str, ConfigKeySpec] = {spec.key: spec for spec in CONFIG_REGISTRY}


# ─── Snapshot ─────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    """Foto inmutable de los overrides vigentes."""

    values: dict[str, Any]
    loaded_at: float

    def is_stale(self, ttl_seconds: int) -> bool:
        return (time.monotonic() - self.loaded_at) >= ttl_seconds


_snapshot: ConfigSnapshot | None = None
_lock = threading.Lock()


async def load_snapshot() -> ConfigSnapshot:
    """Lee todos los overrides de la base y reemplaza el snapshot."""
    values: dict[str, Any] = {}
    try:
        async with session_scope() as session:
            rows = (await session.execute(select(ConfigStoreEntry))).scalars().all()
        for row in rows:
            spec = REGISTRY_BY_KEY.get(row.key)
            if spec is None:
                # Una clave huérfana (parámetro retirado del registry) no debe
                # tumbar la carga: se ignora y se avisa.
                log.warning("config_key_desconocida", key=row.key)
                continue
            try:
                values[row.key] = _coerce(row.value, spec.value_type)
            except ConfigError as exc:
                log.warning("config_valor_invalido", key=row.key, error=str(exc))
    except Exception as exc:  # noqa: BLE001 — sin BD se sirve con los defaults
        log.warning("config_snapshot_failed", error=str(exc))

    snapshot = ConfigSnapshot(values=values, loaded_at=time.monotonic())
    with _lock:
        global _snapshot
        _snapshot = snapshot
    log.debug("config_snapshot_loaded", overrides=len(values))
    return snapshot


def current_snapshot() -> ConfigSnapshot | None:
    with _lock:
        return _snapshot


def invalidate() -> None:
    """Fuerza una recarga en la siguiente lectura. La usa `cli config set`."""
    with _lock:
        global _snapshot
        _snapshot = None


class EffectiveConfig:
    """Proxy de lectura: override de BD si existe, `Settings` si no.

    Deliberadamente síncrono. La recarga por TTL se dispara en segundo plano
    (`asyncio.create_task`) cuando hay un event loop: una lectura nunca se
    bloquea esperando a la base, sólo devuelve el snapshot anterior un
    instante más. Con `refresh()` se puede forzar la recarga y esperarla.
    """

    def __getattr__(self, name: str) -> Any:
        spec = REGISTRY_BY_KEY.get(name)
        if spec is None:
            # No está en el registry: se delega en la capa 1 para que
            # `effective` pueda sustituir a `settings` sin agujeros.
            return getattr(settings, name)

        snapshot = current_snapshot()
        if snapshot is None or snapshot.is_stale(self._ttl_seconds()):
            self._schedule_refresh()

        if snapshot is not None and name in snapshot.values:
            return snapshot.values[name]
        return getattr(settings, name)

    def _ttl_seconds(self) -> int:
        snapshot = current_snapshot()
        if snapshot is not None and "config_cache_ttl_seconds" in snapshot.values:
            ttl: int = snapshot.values["config_cache_ttl_seconds"]
            return ttl
        return getattr(settings, "config_cache_ttl_seconds", DEFAULT_TTL_SECONDS)

    @staticmethod
    def _schedule_refresh() -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # contexto síncrono sin loop: se refrescará más adelante
        loop.create_task(load_snapshot())

    async def refresh(self) -> ConfigSnapshot:
        return await load_snapshot()

    def as_dict(self) -> dict[str, Any]:
        """Valor efectivo de cada clave del registry, con su procedencia."""
        snapshot = current_snapshot()
        overrides = snapshot.values if snapshot else {}
        return {
            spec.key: {
                "valor": overrides.get(spec.key, getattr(settings, spec.key, None)),
                "origen": "config_store" if spec.key in overrides else "settings",
                "grupo": spec.grupo,
                "tipo": spec.value_type,
                "descripcion": spec.description,
            }
            for spec in CONFIG_REGISTRY
        }


effective = EffectiveConfig()


# ─── Escritura ────────────────────────────────────────────────


async def set_value(key: str, raw_value: str, *, motivo: str | None = None, actor: str) -> Any:
    """Fija un override y archiva la versión anterior.

    Devuelve el valor ya convertido. El historial en `config_versions` es lo
    que permite auditar después qué umbral generó una bandera concreta.
    """
    spec = REGISTRY_BY_KEY.get(key)
    if spec is None:
        raise ConfigError(f"'{key}' no está en el registry de configuración")

    value = _coerce(raw_value, spec.value_type)
    stored = _serialize(value)

    async with session_scope() as session:
        entry = await session.get(ConfigStoreEntry, key)
        version = 1 if entry is None else entry.version + 1
        if entry is None:
            session.add(
                ConfigStoreEntry(
                    key=key,
                    value=stored,
                    value_type=spec.value_type,
                    grupo=spec.grupo,
                    version=version,
                    updated_by=actor,
                )
            )
        else:
            entry.value = stored
            entry.version = version
            entry.updated_by = actor
        session.add(
            ConfigVersion(
                key=key,
                value=stored,
                value_type=spec.value_type,
                version=version,
                motivo=motivo,
                updated_by=actor,
            )
        )

    invalidate()
    log.info("config_actualizada", key=key, version=version, actor=actor)
    return value


async def history(key: str) -> list[ConfigVersion]:
    async with session_scope() as session:
        result = await session.execute(
            select(ConfigVersion)
            .where(ConfigVersion.key == key)
            .order_by(ConfigVersion.version.desc())
        )
        return list(result.scalars().all())


__all__ = [
    "CONFIG_REGISTRY",
    "DEFAULT_TTL_SECONDS",
    "REGISTRY_BY_KEY",
    "ConfigError",
    "ConfigKeySpec",
    "ConfigSnapshot",
    "EffectiveConfig",
    "effective",
    "history",
    "invalidate",
    "load_snapshot",
    "set_value",
]
