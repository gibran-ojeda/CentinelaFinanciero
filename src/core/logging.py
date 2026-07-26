"""Logging estructurado con structlog: JSON en producción, legible en desarrollo.

El processor `redact_sensitive` recorre cada evento antes de renderizarlo y
sustituye el valor de cualquier clave que coincida con los patrones de
`settings.log_sensitive_patterns`. Es la última red: los secretos ya son
`SecretStr` en la configuración, pero un dict de headers o un payload de error
puede traer un token que nadie envolvió.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from core.settings import settings

REDACTED = "***"
_MAX_REDACTION_DEPTH = 6

_configured = False


def _is_sensitive(key: str, patterns: tuple[str, ...]) -> bool:
    lowered = key.lower()
    return any(pattern in lowered for pattern in patterns)


def _redact_value(value: Any, patterns: tuple[str, ...], depth: int) -> Any:
    """Redacta recursivamente estructuras anidadas.

    El corte por profundidad evita que un objeto autorreferente cuelgue el
    logger — un log jamás debe poder tumbar el proceso.
    """
    if depth >= _MAX_REDACTION_DEPTH:
        return value
    if isinstance(value, MutableMapping):
        return {
            k: (
                REDACTED
                if isinstance(k, str) and _is_sensitive(k, patterns)
                else _redact_value(v, patterns, depth + 1)
            )
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        redacted = [_redact_value(item, patterns, depth + 1) for item in value]
        return type(value)(redacted) if isinstance(value, tuple) else redacted
    return value


def redact_sensitive(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """Processor de structlog que oculta valores de claves sensibles."""
    patterns = settings.sensitive_patterns
    if not patterns:
        return event_dict
    result = _redact_value(dict(event_dict), patterns, depth=0)
    return dict(result)


def configure_logging(*, force: bool = False) -> None:
    """Configura structlog y el logging estándar. Idempotente."""
    global _configured
    if _configured and not force:
        return

    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        redact_sensitive,
    ]

    renderer: Processor
    if settings.effective_log_format == "json":
        shared.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    # Se enruta por el logging estándar (y no por PrintLogger) para que las
    # librerías —uvicorn, sqlalchemy, apscheduler— compartan destino y nivel,
    # y para que `add_logger_name` tenga de dónde sacar el nombre.
    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level, force=True)
    for noisy in ("uvicorn.access", "sqlalchemy.engine", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Devuelve un logger ya configurado."""
    configure_logging()
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


__all__ = ["REDACTED", "configure_logging", "get_logger", "redact_sensitive"]
