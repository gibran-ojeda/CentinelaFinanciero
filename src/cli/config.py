"""Inspección y ajuste del ConfigStore desde la línea de comandos.

Es la interfaz con la que se mueve un umbral de bandera en producción sin
desplegar. Cada `set` exige un motivo porque el historial de `config_versions`
es lo que permite responder después a "¿por qué esta institución salió marcada
en marzo?" (§19).
"""

from __future__ import annotations

from typing import Any

from core.config_store import (
    CONFIG_REGISTRY,
    REGISTRY_BY_KEY,
    ConfigError,
    effective,
    history,
    set_value,
)


def _formatear(valor: Any) -> str:
    return "—" if valor is None else str(valor)


async def listar(grupo: str | None = None) -> str:
    """Valor efectivo de cada parámetro, con su procedencia."""
    await effective.refresh()
    vista = effective.as_dict()

    lineas: list[str] = []
    grupo_actual = ""
    for spec in CONFIG_REGISTRY:
        if grupo and spec.grupo != grupo:
            continue
        if spec.grupo != grupo_actual:
            grupo_actual = spec.grupo
            lineas.append(f"\n[{grupo_actual}]")
        info = vista[spec.key]
        marca = "*" if info["origen"] == "config_store" else " "
        lineas.append(f" {marca} {spec.key:<38} {_formatear(info['valor']):>14}")
        lineas.append(f"     {spec.description}")

    if not lineas:
        return f"No hay parámetros en el grupo '{grupo}'."

    lineas.append("\n  (*) valor sobrescrito en la base; el resto viene de Settings")
    return "\n".join(lineas).lstrip("\n")


async def fijar(key: str, valor: str, *, motivo: str, actor: str) -> str:
    if key not in REGISTRY_BY_KEY:
        cercanas = [k for k in REGISTRY_BY_KEY if key.split("_")[0] in k]
        sugerencia = f" ¿Quisiste decir: {', '.join(sorted(cercanas))}?" if cercanas else ""
        raise ConfigError(f"'{key}' no es un parámetro configurable.{sugerencia}")

    anterior = getattr(effective, key)
    nuevo = await set_value(key, valor, motivo=motivo, actor=actor)
    await effective.refresh()
    return f"{key}: {_formatear(anterior)} → {_formatear(nuevo)}  (motivo: {motivo})"


async def historial(key: str) -> str:
    if key not in REGISTRY_BY_KEY:
        raise ConfigError(f"'{key}' no es un parámetro configurable")

    versiones = await history(key)
    if not versiones:
        return f"{key} no tiene overrides: siempre ha usado el valor de Settings."

    lineas = [f"Historial de {key}:"]
    for v in versiones:
        cuando = v.created_at.strftime("%Y-%m-%d %H:%M")
        lineas.append(
            f"  v{v.version:<3} {v.value:>12}  {cuando}  por {v.updated_by or '—'}"
            f"  {v.motivo or ''}"
        )
    return "\n".join(lineas)


__all__ = ["fijar", "historial", "listar"]
