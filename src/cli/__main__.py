"""CLI de operación: `python -m cli <comando>`.

Es la interfaz de administración del MVP. La fase conceptual F1 del foundation
pide datos manuales actualizados semanalmente, así que esto no es una utilidad
de desarrollo: es la herramienta con la que se opera el producto hasta que las
ingestas automáticas de las fases 7-9 lo releven.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date
from pathlib import Path

from core.db import dispose_engine
from core.logging import configure_logging, get_logger
from domain.enums import EstadoRevision

log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cli",
        description="Herramientas de operación de Centinela Financiero.",
    )
    sub = parser.add_subparsers(dest="comando", required=True)

    seed = sub.add_parser("seed", help="carga idempotente de los catálogos semilla")
    seed.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="directorio de semillas (por defecto: seeds/ del repo)",
    )

    tasas = sub.add_parser("tasas", help="alta y consulta de tasas")
    tasas_sub = tasas.add_subparsers(dest="subcomando", required=True)
    importar = tasas_sub.add_parser("import", help="alta de tasas desde un CSV")
    importar.add_argument("csv", type=Path, help="archivo CSV de observaciones")
    importar.add_argument(
        "--dry-run",
        action="store_true",
        help="valida y reporta sin escribir nada",
    )
    tasas_sub.add_parser(
        "pendientes",
        help="lista de revisión: qué falta verificar para que salga al sitio público",
    )
    fetch = tasas_sub.add_parser(
        "fetch",
        help="lee las páginas de las instituciones y encola lo que cambió",
    )
    # El job del VPS corre `--sin-navegador`; la pasada de las páginas con
    # JavaScript se hace desde local con `--solo-navegador` mientras Chromium
    # no viva en la imagen. Ver docs/despliegue.md.
    grupo = fetch.add_mutually_exclusive_group()
    grupo.add_argument(
        "--solo-navegador",
        action="store_true",
        help="sólo las fuentes que necesitan JavaScript",
    )
    grupo.add_argument(
        "--sin-navegador",
        action="store_true",
        help="sólo las fuentes que rinden a un cliente HTTP plano",
    )

    banxico = sub.add_parser("banxico", help="ingesta del SIE de Banxico")
    banxico_sub = banxico.add_subparsers(dest="subcomando", required=True)
    banxico_sync = banxico_sub.add_parser(
        "sync",
        help="trae las series del SIE y publica las subastas de CETES",
    )
    # La sincronización arranca desde lo último guardado, así que nunca
    # alcanzaría un hueco anterior. Esto es para rellenarlo a mano.
    banxico_sync.add_argument(
        "--desde",
        type=date.fromisoformat,
        default=None,
        metavar="AAAA-MM-DD",
        help="fuerza el inicio del rango para todas las series",
    )

    revs = sub.add_parser("revisiones", help="cola de revisión humana de tasas")
    revs_sub = revs.add_subparsers(dest="subcomando", required=True)

    listar_revs = revs_sub.add_parser("list", help="pendientes, con su diferencia y su fuente")
    listar_revs.add_argument(
        "--estado",
        type=EstadoRevision,
        choices=list(EstadoRevision),
        default=EstadoRevision.PENDIENTE,
    )

    for accion, ayuda in (("approve", "publica la tasa"), ("reject", "la descarta")):
        parser_accion = revs_sub.add_parser(accion, help=ayuda)
        parser_accion.add_argument("revision_id", type=int)
        # Quién decidió es parte del registro: §19 pide poder reconstruir por
        # qué una tasa está publicada, y "alguien" no reconstruye nada.
        parser_accion.add_argument(
            "--revisor", default=os.environ.get("USER", "cli"), help="quién resuelve"
        )
        parser_accion.add_argument("--comentario", default=None)

    config = sub.add_parser("config", help="inspección y ajuste del ConfigStore")
    config_sub = config.add_subparsers(dest="subcomando", required=True)

    listar = config_sub.add_parser("list", help="valor efectivo de cada parámetro")
    listar.add_argument("--grupo", default=None, help="banderas | fiscal | revision | scheduler")

    fijar = config_sub.add_parser("set", help="sobrescribe un parámetro en caliente")
    fijar.add_argument("key")
    fijar.add_argument("valor")
    # El motivo es obligatorio: sin él, el historial de config_versions no
    # sirve para reconstruir por qué una institución salió marcada (§19).
    fijar.add_argument("--motivo", required=True, help="por qué se cambia (queda en el historial)")
    fijar.add_argument("--actor", default=os.environ.get("USER", "cli"), help="quién lo cambia")

    historial = config_sub.add_parser("history", help="historial de cambios de un parámetro")
    historial.add_argument("key")

    return parser


async def _run(args: argparse.Namespace) -> int:
    from cli import config as config_module
    from cli import seed as seed_module
    from cli import tasas as tasas_module

    match args.comando:
        case "seed":
            report = await seed_module.run_seed(args.dir)
            print("Carga de catálogos:")
            print(report.render())
            return 0
        case "tasas":
            if args.subcomando == "pendientes":
                lista = await tasas_module.listar_pendientes()
                print("Pendientes de verificar contra la fuente oficial:")
                print(lista.render())
                return 0
            if args.subcomando == "fetch":
                reporte = await tasas_module.correr_fetch(
                    solo_navegador=args.solo_navegador, sin_navegador=args.sin_navegador
                )
                print("Lectura de tasas:")
                print(reporte.render())
                # Que una fuente falle no es un fallo del comando: se reporta y
                # la siguiente corrida lo reintenta. Sólo el techo de gasto,
                # que corta la corrida a medias, merece salida distinta de 0.
                return 1 if reporte.presupuesto_agotado else 0
            resultado = await tasas_module.import_csv(args.csv, dry_run=args.dry_run)
            titulo = "Simulación de alta de tasas" if args.dry_run else "Alta de tasas"
            print(f"{titulo}:")
            print(resultado.render())
            # Una fila rechazada es un fallo operativo: el CSV traía un dato
            # que no se pudo cargar y alguien tiene que enterarse.
            return 1 if resultado.errores else 0
        case "banxico":
            from cli import banxico as banxico_module

            reporte_banxico = await banxico_module.correr_sync(desde=args.desde)
            print("Ingesta de Banxico:")
            print(reporte_banxico.render())
            # Un lote que el SIE no atendió es un fallo operativo: la serie se
            # queda vieja y alguien tiene que enterarse hoy, no en la portada.
            return 1 if reporte_banxico.hubo_errores else 0
        case "revisiones":
            from cli import revisiones as revisiones_module

            if args.subcomando == "list":
                print("Cola de revisión:")
                print(await revisiones_module.listar(args.estado))
                return 0
            print(
                await revisiones_module.resolver(
                    args.revision_id,
                    aprobar=args.subcomando == "approve",
                    revisor=args.revisor,
                    comentario=args.comentario,
                )
            )
            return 0
        case "config":
            match args.subcomando:
                case "list":
                    print(await config_module.listar(args.grupo))
                case "set":
                    print(
                        await config_module.fijar(
                            args.key, args.valor, motivo=args.motivo, actor=args.actor
                        )
                    )
                case "history":
                    print(await config_module.historial(args.key))
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
