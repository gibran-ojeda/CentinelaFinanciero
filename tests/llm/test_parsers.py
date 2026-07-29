"""Tests del parseo de lo que contesta un modelo.

Cada caso de aquí salió de una forma real en la que un modelo devuelve JSON
válido envuelto en algo que no lo es. El criterio: limpiar el envoltorio sí,
adivinar el contenido nunca.
"""

from __future__ import annotations

import pytest

from llm.parsers import limpiar, parsear_json
from llm.providers.base import ErrorDeParseo


def test_plain_json_parses() -> None:
    assert parsear_json('{"tasa": 8.69}') == {"tasa": 8.69}


def test_markdown_fences_are_stripped() -> None:
    assert parsear_json('```json\n{"tasa": 8.69}\n```') == {"tasa": 8.69}
    assert parsear_json('```\n{"tasa": 8.69}\n```') == {"tasa": 8.69}


def test_a_reasoning_block_is_dropped() -> None:
    crudo = '<think>a ver, la tabla dice 8.69</think>\n{"tasa": 8.69}'
    assert parsear_json(crudo) == {"tasa": 8.69}


def test_prose_around_the_object_is_ignored() -> None:
    crudo = 'Claro, aquí tienes:\n{"tasa": 8.69}\nEspero que te sirva.'
    assert parsear_json(crudo) == {"tasa": 8.69}


def test_nested_objects_survive() -> None:
    """Contar llaves y no usar una expresión regular: el JSON anida."""
    crudo = '{"producto": {"plazo": {"dias": 360}}, "tasa": 8.69}'
    assert parsear_json(crudo)["producto"]["plazo"]["dias"] == 360


def test_braces_inside_strings_do_not_break_the_count() -> None:
    """Es donde la cuenta ingenua de llaves se rompe."""
    crudo = '{"nota": "el sitio dice {tasa} sin sustituir", "tasa": 8.69}'
    assert parsear_json(crudo)["tasa"] == 8.69


def test_the_reasoning_channel_is_the_fallback() -> None:
    """Un razonador que gastó el presupuesto pensando deja el contenido vacío.

    El JSON quedó en el otro canal; descartarlo sería tirar una extracción que
    sí se hizo.
    """
    assert parsear_json("", respaldo='{"tasa": 8.69}') == {"tasa": 8.69}


def test_missing_required_keys_is_an_error_not_a_default() -> None:
    """Un parser que rellena huecos convierte un error visible en un invento."""
    with pytest.raises(ErrorDeParseo, match="faltan claves"):
        parsear_json('{"tasa": 8.69}', claves_requeridas=("tasa", "plazo_dias"))


def test_no_json_at_all_raises_with_the_raw_content() -> None:
    with pytest.raises(ErrorDeParseo) as exc:
        parsear_json("No encontré tasas en esta página.")
    assert "No encontré tasas" in exc.value.contenido_crudo


def test_a_json_array_is_not_an_object() -> None:
    """El contrato es un objeto: una lista suelta no cumple y no se fuerza."""
    with pytest.raises(ErrorDeParseo):
        parsear_json("[1, 2, 3]")


def test_limpiar_leaves_clean_text_alone() -> None:
    assert limpiar('{"a": 1}') == '{"a": 1}'
