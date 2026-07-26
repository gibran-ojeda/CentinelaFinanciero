"""Punto de entrada del servicio API: `python -m api`."""

from __future__ import annotations

import uvicorn

from core.logging import configure_logging
from core.settings import settings


def main() -> None:
    configure_logging()
    uvicorn.run(
        "api.app:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,  # el logging ya lo configura structlog
        access_log=not settings.is_production,
    )


if __name__ == "__main__":
    main()
