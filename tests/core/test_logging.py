"""Tests del logging estructurado y de la redacción de secretos."""

from __future__ import annotations

import json
from typing import Any

import structlog

from core.logging import REDACTED, configure_logging, get_logger, redact_sensitive


def _redact(event: dict[str, Any]) -> dict[str, Any]:
    return redact_sensitive(None, "info", event)  # type: ignore[arg-type]


def test_redacts_top_level_sensitive_keys() -> None:
    out = _redact({"event": "login", "password": "hunter2", "api_key": "sk-123"})
    assert out["password"] == REDACTED
    assert out["api_key"] == REDACTED
    assert out["event"] == "login"


def test_redaction_matches_by_substring() -> None:
    out = _redact({"deepseek_api_key": "sk-x", "Authorization": "Bearer y", "banxico_token": "t"})
    assert out["deepseek_api_key"] == REDACTED
    assert out["Authorization"] == REDACTED
    assert out["banxico_token"] == REDACTED


def test_redacts_nested_structures() -> None:
    out = _redact(
        {
            "event": "request",
            "headers": {"authorization": "Bearer abc", "accept": "json"},
            "items": [{"token": "t1"}, {"nombre": "FinSUS"}],
        }
    )
    assert out["headers"]["authorization"] == REDACTED
    assert out["headers"]["accept"] == "json"
    assert out["items"][0]["token"] == REDACTED
    assert out["items"][1]["nombre"] == "FinSUS"


def test_non_sensitive_payload_is_untouched() -> None:
    event = {"event": "tasa_cargada", "institucion": "CETES", "tasa_nominal": "7.5"}
    assert _redact(event) == event


def test_deep_recursion_is_bounded() -> None:
    """Un log jamás debe poder colgar el proceso por una estructura profunda."""
    deep: dict[str, Any] = {"password": "leaf"}
    for _ in range(50):
        deep = {"nivel": deep}
    # No debe lanzar ni entrar en recursión infinita.
    assert _redact({"event": "x", "payload": deep})["event"] == "x"


def test_json_renderer_in_production(capsys: Any) -> None:
    from core.settings import Settings
    import core.logging as logging_module

    original = logging_module.settings
    logging_module.settings = Settings(environment="prod", log_level="INFO")  # type: ignore[misc]
    try:
        configure_logging(force=True)
        structlog.get_logger("test").info("tasa_publicada", api_key="sk-secreto", plazo=28)
        captured = capsys.readouterr().err.strip().splitlines()[-1]
        payload = json.loads(captured)
        assert payload["event"] == "tasa_publicada"
        assert payload["api_key"] == REDACTED
        assert payload["plazo"] == 28
    finally:
        logging_module.settings = original  # type: ignore[misc]
        configure_logging(force=True)


def test_get_logger_returns_usable_logger() -> None:
    logger = get_logger("brujula.test")
    logger.info("evento_de_prueba", detalle="ok")
