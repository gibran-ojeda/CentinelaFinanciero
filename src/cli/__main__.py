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
    retirar = tasas_sub.add_parser(
        "retirar",
        help="comenta en el CSV semilla las filas de agregador ya sustituidas por lectura oficial",
    )
    retirar.add_argument(
        "--csv",
        type=Path,
        default=Path("seeds/tasas.csv"),
        help="archivo a podar (default: seeds/tasas.csv)",
    )
    retirar.add_argument("--dry-run", action="store_true", help="reporta sin escribir")
    fetch = tasas_sub.add_parser(
        "fetch",
        help="lee las páginas de las instituciones y encola lo que cambió",
    )
    # Filtros de depuración: el job programado lee todo con su propia cadena
    # (httpx + navegador). Estos dos sirven para repetir a mano una mitad —
    # p. ej. reintentar sólo las páginas JS tras un fallo puntual.
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

    cnbv = sub.add_parser("cnbv", help="ingesta de boletines de la CNBV")
    cnbv_sub = cnbv.add_subparsers(dest="subcomando", required=True)
    cnbv_cargar = cnbv_sub.add_parser(
        "cargar",
        help="descarga el último boletín publicado y recomputa las banderas",
    )
    cnbv_cargar.add_argument(
        "--forzar",
        action="store_true",
        help="vuelve a cargar el último periodo aunque ya esté (p. ej. tras corregir un mapeo)",
    )

    research = sub.add_parser("research", help="calibración de la búsqueda abierta (nivel 3)")
    research_sub = research.add_subparsers(dest="subcomando", required=True)
    research_reporte = research_sub.add_parser(
        "reporte",
        help="costo semanal, huecos y tasa de aprobación por fuente",
    )
    research_reporte.add_argument(
        "--semanas", type=int, default=4, help="ventana hacia atrás (default 4)"
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

    fuentes = sub.add_parser("fuentes", help="salud y reparación del catálogo de fuentes")
    fuentes_sub = fuentes.add_subparsers(dest="subcomando", required=True)

    listar_fuentes = fuentes_sub.add_parser(
        "list", help="cada fuente con su estado, su último dato y su último error"
    )
    listar_fuentes.add_argument(
        "--rotas",
        action="store_true",
        help="sólo las pausadas, las que acumulan fallos y las que nunca dieron una tasa",
    )

    pausar_fuente = fuentes_sub.add_parser("pausar", help="deja de intentarla en cada corrida")
    pausar_fuente.add_argument("fuente_id", type=int)
    # Igual que en `config set`: sin motivo, dentro de un mes nadie sabrá si la
    # apagó una persona por algo o el contador de fallos.
    pausar_fuente.add_argument("--motivo", required=True, help="por qué se pausa")

    reanudar_fuente = fuentes_sub.add_parser(
        "reanudar", help="vuelve a intentarla y olvida los fallos acumulados"
    )
    reanudar_fuente.add_argument("fuente_id", type=int)

    url_fuente = fuentes_sub.add_parser(
        "url", help="corrige la URL en su sitio (provisional hasta llevarlo al YAML)"
    )
    url_fuente.add_argument("fuente_id", type=int)
    url_fuente.add_argument("url")

    probar_fuente = fuentes_sub.add_parser(
        "probar", help="descarga con cada transporte y dice si de verdad necesita navegador"
    )
    # Sin id se prueban todas. No toca la base, así que no hace falta gate.
    probar_fuente.add_argument("fuente_id", type=int, nargs="?", default=None)
    probar_fuente.add_argument(
        "--extraer",
        action="store_true",
        help="además pasa cada texto al extractor y cuenta tasas (cuesta tokens)",
    )

    purgar_fuentes = fuentes_sub.add_parser(
        "purgar",
        help="borra las que el YAML dejó de declarar y nunca produjeron una tasa",
    )
    purgar_fuentes.add_argument(
        "--dry-run", action="store_true", help="lista lo que borraría, sin borrar"
    )

    config = sub.add_parser("config", help="inspección y ajuste del ConfigStore")
    config_sub = config.add_subparsers(dest="subcomando", required=True)

    listar = config_sub.add_parser("list", help="valor efectivo de cada parámetro")
    listar.add_argument(
        "--grupo", default=None, help="banderas | fiscal | llm | revision | scheduler"
    )

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
            if args.subcomando == "retirar":
                reporte_retiro = await tasas_module.retirar_sustituidas(
                    args.csv, dry_run=args.dry_run
                )
                titulo = (
                    "Simulación de retiro de agregador"
                    if args.dry_run
                    else "Retiro de filas de agregador sustituidas"
                )
                print(f"{titulo}:")
                print(reporte_retiro.render())
                return 0
            if args.subcomando == "fetch":
                reporte = await tasas_module.correr_fetch(
                    solo_navegador=args.solo_navegador, sin_navegador=args.sin_navegador
                )
                print("Lectura de tasas:")
                print(reporte.render())
                # Que una fuente falle no es un fallo del comando: se reporta y
                # la siguiente corrida lo reintenta. Salida distinta de 0 sólo
                # cuando no hubo corrida que valga: el techo de gasto la cortó
                # a medias, o todas las fuentes fallaron.
                return 1 if reporte.presupuesto_agotado or reporte.fracaso_total else 0
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
        case "cnbv":
            from ingest_cnbv import loader

            reporte_cnbv = await loader.cargar(forzar=args.forzar)
            print("Ingesta de la CNBV:")
            print(reporte_cnbv.render())
            # Un cambio de formato es un fallo operativo: alguien tiene que
            # mirar el boletín y ajustar la declaración de `fuentes.py`.
            return 1 if reporte_cnbv.hubo_errores else 0
        case "research":
            from cli import research as research_module

            print("Calibración del researcher:")
            print((await research_module.reporte(args.semanas)).render())
            return 0
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
        case "fuentes":
            from cli import fuentes as fuentes_module

            match args.subcomando:
                case "list":
                    print("Catálogo de fuentes:")
                    print(await fuentes_module.listar(solo_rotas=args.rotas))
                case "pausar":
                    print(await fuentes_module.pausar(args.fuente_id, motivo=args.motivo))
                case "reanudar":
                    print(await fuentes_module.reanudar(args.fuente_id))
                case "url":
                    print(await fuentes_module.cambiar_url(args.fuente_id, args.url))
                case "probar":
                    print("Transporte que necesita cada fuente:")
                    print(await fuentes_module.probar(args.fuente_id, extraer_tasas=args.extraer))
                case "purgar":
                    print("Purga de fuentes retiradas del catálogo:")
                    print(await fuentes_module.purgar(dry_run=args.dry_run))
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
