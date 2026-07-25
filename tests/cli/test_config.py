"""Tests de la interfaz de configuración.

Cubren el caso de uso completo del criterio de aceptación: cambiar un umbral
por CLI y que se refleje sin reiniciar, con historial auditable.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from cli.config import fijar, historial, listar
from core.config_store import ConfigError, effective
from core.settings import settings

pytestmark = [pytest.mark.requires_docker, pytest.mark.usefixtures("real_db")]


async def test_list_shows_every_parameter_with_its_origin() -> None:
    salida = await listar()

    assert "umbral_imor_roja" in salida
    assert "[banderas]" in salida
    assert "[fiscal]" in salida
    assert "[revision]" in salida
    assert "[scheduler]" in salida
    # Sin overrides todavía: ninguna línea marcada con asterisco.
    assert "\n * " not in salida


async def test_list_can_be_filtered_by_group() -> None:
    salida = await listar("banderas")
    assert "umbral_imor_roja" in salida
    assert "cache_comparador_ttl_seconds" not in salida


async def test_unknown_group_is_reported() -> None:
    assert "no hay parámetros" in (await listar("inventado")).lower()


async def test_set_changes_the_effective_value_without_restart() -> None:
    """El criterio de aceptación de la fase 2, extremo a extremo."""
    assert effective.umbral_imor_roja == settings.umbral_imor_roja

    salida = await fijar(
        "umbral_imor_roja", "5.5", motivo="calibración tras revisar CNBV", actor="gibran"
    )

    assert "6.0" in salida and "5.5" in salida
    assert effective.umbral_imor_roja == Decimal("5.5")


async def test_overridden_values_are_marked_in_the_listing() -> None:
    await fijar("umbral_imor_roja", "5.5", motivo="prueba", actor="test")
    salida = await listar("banderas")

    lineas = [linea for linea in salida.splitlines() if "umbral_imor_roja" in linea]
    assert lineas and lineas[0].lstrip().startswith("*")


async def test_unknown_key_suggests_alternatives() -> None:
    with pytest.raises(ConfigError, match="umbral_"):
        await fijar("umbral_imor_rojo", "5.5", motivo="typo", actor="test")


async def test_history_records_who_changed_what_and_why() -> None:
    """§19: reconstruir qué umbral estaba vigente cuándo y por qué."""
    await fijar("umbral_icap_roja", "11.0", motivo="ajuste regulatorio", actor="gibran")
    await fijar("umbral_icap_roja", "10.5", motivo="revertido", actor="gibran")

    salida = await historial("umbral_icap_roja")

    assert "v2" in salida and "v1" in salida
    assert "ajuste regulatorio" in salida
    assert "revertido" in salida
    assert "gibran" in salida
    # El más reciente primero.
    assert salida.index("revertido") < salida.index("ajuste regulatorio")


async def test_history_of_an_untouched_parameter_says_so() -> None:
    salida = await historial("umbral_imor_amarilla")
    assert "Settings" in salida


async def test_history_rejects_unknown_keys() -> None:
    with pytest.raises(ConfigError):
        await historial("no_existe")
