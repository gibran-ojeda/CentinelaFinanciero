"""Logging estructurado con structlog: JSON en producción, legible en desarrollo.

El processor `redact_sensitive` recorre cada evento antes de renderizarlo y
sustituye el valor de cualquier clave que coincida con los patrones de
`settings.log_sensitive_patterns`. Es la última red: los secretos ya son
`SecretStr` en la configuración, pero un dict de headers o un payload de error
puede traer un token que nadie envolvió.

Y esa red cubre también lo que escriben las librerías. El renderizado vive en
un `ProcessorFormatter` del handler raíz, con la misma cadena de processors
como `foreign_pre_chain`, de modo que un `LogRecord` de httpx o de trafilatura
sale en el mismo JSON y pasa por la misma redacción. Antes no: el handler
formateaba con `%(message)s` a secas y todo lo ajeno salía crudo por el mismo
stderr, esquivando la última red — un agujero, no sólo un problema de estética.
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
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # **El renderizado ocurre en el handler, no dentro de structlog.** Con
    # `format="%(message)s"` a secas, un `LogRecord` de httpx o de trafilatura
    # salía crudo por el mismo stderr: sin timestamp, sin nivel, sin nombre de
    # logger, y —lo que importa— **sin pasar por `redact_sensitive`**, que es
    # un processor de structlog y no tocaba nada ajeno. `foreign_pre_chain` les
    # aplica la misma cadena, así que la última red cubre también lo que
    # escriben las librerías.
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=renderer,
            # `ExtraAdder` va **antes** que la cadena, y por tanto antes que
            # `redact_sensitive`: lo que una librería mande por `extra=` son
            # atributos del `LogRecord` que sin esto no se verían siquiera, y
            # con esto se ven ya redactados. Ése es el orden que importa.
            foreign_pre_chain=[structlog.stdlib.ExtraAdder(), *shared],
        )
    )
    logging.basicConfig(handlers=[handler], level=level, force=True)

    # Y aun con formato decente, estas hablan demasiado para un job que lee
    # dieciocho páginas: httpx emite una línea INFO por petición —incluidas las
    # de robots.txt y todas las del LLM— y trafilatura avisa en WARNING cada vez
    # que descarta una página, que es justo lo que `fetch_vacio` ya reporta.
    silenciar = {
        "uvicorn.access": logging.WARNING,
        "sqlalchemy.engine": logging.WARNING,
        "apscheduler.executors.default": logging.WARNING,
        "httpx": logging.WARNING,
        "httpcore": logging.WARNING,
        "urllib3": logging.WARNING,
        "playwright": logging.WARNING,
        "ddgs": logging.WARNING,
        "trafilatura": logging.ERROR,
    }
    for nombre, minimo in silenciar.items():
        logging.getLogger(nombre).setLevel(max(level, minimo))

    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Devuelve un logger ya configurado."""
    configure_logging()
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


__all__ = ["REDACTED", "configure_logging", "get_logger", "redact_sensitive"]
