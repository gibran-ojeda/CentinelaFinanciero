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


def test_the_token_count_is_not_a_token() -> None:
    """La métrica con la que se calibra el gasto del LLM salía redactada.

    El patrón `token` está en la lista por `BANXICO_TOKEN` y la coincidencia es
    por subcadena, así que `tokens` caía dentro. Todo el log de producción
    decía `"tokens": "***"` y no había forma de saber por qué una llamada costó
    cuatro veces más que la siguiente.
    """
    out = _redact(
        {
            "event": "llm_respuesta",
            "tokens": 1420,
            "tokens_entrada": 1200,
            "tokens_salida": 220,
            "costo_usd": 0.001125,
        }
    )

    assert out["tokens"] == 1420
    assert out["tokens_entrada"] == 1200
    assert out["tokens_salida"] == 220


def test_the_exemption_is_exact_and_does_not_leak_the_real_thing() -> None:
    """La exención es por clave completa, no por prefijo.

    Si fuera por prefijo, un `tokens_api_key` pasaría entero. Y las llaves que
    motivaron el patrón siguen redactadas.
    """
    out = _redact(
        {
            "banxico_token": "abc123",
            "access_token": "xyz789",
            "tokens_api_key": "sk-secreto",
            "tokens": 10,
        }
    )

    assert out["banxico_token"] == REDACTED
    assert out["access_token"] == REDACTED
    assert out["tokens_api_key"] == REDACTED
    assert out["tokens"] == 10


def test_deep_recursion_is_bounded() -> None:
    """Un log jamás debe poder colgar el proceso por una estructura profunda."""
    deep: dict[str, Any] = {"password": "leaf"}
    for _ in range(50):
        deep = {"nivel": deep}
    # No debe lanzar ni entrar en recursión infinita.
    assert _redact({"event": "x", "payload": deep})["event"] == "x"


def test_json_renderer_in_production(capsys: Any) -> None:
    import core.logging as logging_module
    from core.settings import Settings

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
    logger = get_logger("centinela.test")
    logger.info("evento_de_prueba", detalle="ok")


# ─── Lo que escriben las librerías ────────────────────────────


def _en_produccion(cuerpo: Any) -> None:
    """Corre `cuerpo()` con el logging de producción y lo deja como estaba."""
    import core.logging as logging_module
    from core.settings import Settings

    original = logging_module.settings
    logging_module.settings = Settings(environment="prod", log_level="INFO")  # type: ignore[misc]
    try:
        configure_logging(force=True)
        cuerpo()
    finally:
        logging_module.settings = original  # type: ignore[misc]
        configure_logging(force=True)


def test_a_third_party_record_is_rendered_and_redacted_too(capsys: Any) -> None:
    """El agujero que esto cierra no era de estética.

    `redact_sensitive` es un processor de structlog, así que sólo veía lo que
    pasaba por structlog. Un `LogRecord` de una librería salía por el mismo
    stderr con `%(message)s` a secas: crudo, sin nivel ni logger, y **sin pasar
    por la última red**. Ahora lo formatea el mismo `ProcessorFormatter`.
    """
    import logging as stdlib_logging

    def cuerpo() -> None:
        stdlib_logging.getLogger("libreria.ajena").warning(
            "conectando", extra={"api_key": "sk-secreto"}
        )

    _en_produccion(cuerpo)

    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert payload["event"] == "conectando"
    assert payload["level"] == "warning"
    assert payload["logger"] == "libreria.ajena"
    assert payload["api_key"] == REDACTED


def test_the_chatty_libraries_are_quiet_at_info(capsys: Any) -> None:
    """httpx emite una línea por petición: las de robots.txt, las del fetch y
    todas las del LLM. En una corrida de dieciocho fuentes son cientos de
    líneas que tapan los eventos propios."""
    import logging as stdlib_logging

    def cuerpo() -> None:
        stdlib_logging.getLogger("httpx").info('HTTP Request: GET https://x.test "200 OK"')
        # trafilatura avisa en WARNING cada vez que descarta una página, que es
        # justo lo que `fetch_vacio` ya reporta con la URL.
        stdlib_logging.getLogger("trafilatura.core").warning("discarding data: None")

    _en_produccion(cuerpo)

    assert capsys.readouterr().err.strip() == ""
