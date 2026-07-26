"""Punto de entrada del servicio scheduler: `python -m scheduler`."""

from __future__ import annotations

import asyncio

from core.logging import configure_logging
from scheduler.runner import run_forever


def main() -> None:
    configure_logging()
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
