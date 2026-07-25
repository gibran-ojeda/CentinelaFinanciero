"""CLI de operación: `python -m cli <comando>`.

Es la interfaz de administración del MVP. La fase conceptual F1 del foundation
pide datos manuales actualizados semanalmente, así que esto no es una utilidad
de desarrollo: es la herramienta con la que se opera el producto hasta que las
ingestas automáticas de las fases 7-9 lo releven.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from core.db import dispose_engine
from core.logging import configure_logging, get_logger

log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cli",
        description="Herramientas de operación de Brújula Financiera.",
    )
    sub = parser.add_subparsers(dest="comando", required=True)

    seed = sub.add_parser("seed", help="carga idempotente de los catálogos semilla")
    seed.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="directorio de semillas (por defecto: seeds/ del repo)",
    )

    return parser


async def _run(args: argparse.Namespace) -> int:
    from cli import seed as seed_module

    match args.comando:
        case "seed":
            report = await seed_module.run_seed(args.dir)
            print("Carga de catálogos:")
            print(report.render())
            return 0
        case _:  # pragma: no cover — argparse ya lo impide
            return 2


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)

    async def _main() -> int:
        try:
            return await _run(args)
        finally:
            await dispose_engine()

    try:
        return asyncio.run(_main())
    except Exception as exc:  # noqa: BLE001 — la CLI reporta, no vuelca traza
        log.error("cli_error", comando=args.comando, error=str(exc))
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
