"""Alta manual de tasas desde CSV.

Es la vía de carga del MVP: la fase conceptual F1 opera con datos manuales
actualizados semanalmente. Cuando lleguen las ingestas automáticas (fases 7-9)
seguirá siendo la vía de corrección y de alta de instituciones nuevas.

Semántica **append-only**: cada fila del CSV es una observación. Nunca se
modifica ni se borra una tasa anterior — la vigente de un producto es la más
reciente en estado VIGENTE. Reimportar el mismo CSV no duplica nada porque la
clave natural es (producto, fecha_dato, fuente).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import select

from core.db import session_scope
from core.logging import get_logger
from domain.enums import EstadoTasa, FuenteTasa
from domain.orm import Producto, Tasa

log = get_logger(__name__)

COLUMNAS_REQUERIDAS = {"producto_slug", "tasa_nominal", "fecha_dato"}

#: Una tasa por encima de esto casi seguro es un error de captura (un 950 en
#: vez de 9.50). Se rechaza la fila en vez de publicar un disparate.
TASA_MAXIMA_PLAUSIBLE = Decimal("100")


class ImportError_(Exception):
    """CSV mal formado o con filas no procesables."""


@dataclass(slots=True)
class ImportReport:
    creadas: int = 0
    duplicadas: int = 0
    errores: list[str] = field(default_factory=list)
    por_estado: dict[str, int] = field(default_factory=dict)

    def render(self) -> str:
        lineas = [
            f"  altas nuevas         {self.creadas:>4}",
            f"  ya existentes        {self.duplicadas:>4}",
        ]
        for estado, total in sorted(self.por_estado.items()):
            lineas.append(f"  en estado {estado:<12} {total:>4}")
        if self.errores:
            lineas.append(f"  filas rechazadas     {len(self.errores):>4}")
            lineas.extend(f"    - {e}" for e in self.errores)
        return "\n".join(lineas)


def _decimal(raw: str, campo: str, fila: int) -> Decimal | None:
    valor = (raw or "").strip()
    if not valor:
        return None
    try:
        return Decimal(valor)
    except InvalidOperation as exc:
        raise ImportError_(f"fila {fila}: '{campo}' no es un número ('{valor}')") from exc


def _fecha(raw: str, fila: int) -> date:
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ImportError_(f"fila {fila}: 'fecha_dato' debe ser YYYY-MM-DD ('{raw}')") from exc


async def import_csv(path: Path, *, dry_run: bool = False) -> ImportReport:
    """Da de alta las observaciones del CSV. Una transacción: todo o nada."""
    if not path.exists():
        raise ImportError_(f"no existe el archivo {path}")

    with path.open(encoding="utf-8", newline="") as handle:
        filas = list(csv.DictReader(handle))

    if not filas:
        raise ImportError_(f"{path.name} no tiene filas")

    faltantes = COLUMNAS_REQUERIDAS - set(filas[0])
    if faltantes:
        raise ImportError_(f"{path.name}: faltan columnas {sorted(faltantes)}")

    report = ImportReport()

    async with session_scope() as session:
        productos = {row.slug: row for row in (await session.execute(select(Producto))).scalars()}
        existentes = {
            (row.producto_id, row.fecha_dato, row.fuente)
            for row in (await session.execute(select(Tasa))).scalars()
        }

        for numero, fila in enumerate(filas, start=2):
            slug = (fila.get("producto_slug") or "").strip()
            if not slug or slug.startswith("#"):
                continue

            producto = productos.get(slug)
            if producto is None:
                report.errores.append(f"fila {numero}: producto desconocido '{slug}'")
                continue

            tasa_nominal = _decimal(fila["tasa_nominal"], "tasa_nominal", numero)
            if tasa_nominal is None:
                report.errores.append(f"fila {numero}: 'tasa_nominal' vacía")
                continue
            if tasa_nominal < 0 or tasa_nominal > TASA_MAXIMA_PLAUSIBLE:
                report.errores.append(
                    f"fila {numero}: tasa fuera de rango plausible ({tasa_nominal}%). "
                    f"Se rechaza en vez de publicar un dato imposible."
                )
                continue

            fecha_dato = _fecha(fila["fecha_dato"], numero)
            if fecha_dato > date.today():
                report.errores.append(
                    f"fila {numero}: fecha_dato en el futuro ({fecha_dato.isoformat()})"
                )
                continue

            fuente = FuenteTasa((fila.get("fuente") or "MANUAL").strip() or "MANUAL")
            estado = EstadoTasa((fila.get("estado") or "VIGENTE").strip() or "VIGENTE")

            clave = (producto.id, fecha_dato, fuente)
            if clave in existentes:
                report.duplicadas += 1
                continue

            session.add(
                Tasa(
                    producto_id=producto.id,
                    tasa_nominal=tasa_nominal,
                    gat_nominal=_decimal(fila.get("gat_nominal", ""), "gat_nominal", numero),
                    gat_real=_decimal(fila.get("gat_real", ""), "gat_real", numero),
                    fecha_dato=fecha_dato,
                    fuente=fuente,
                    fuente_url=(fila.get("fuente_url") or "").strip() or None,
                    estado=estado,
                    notas=(fila.get("notas") or "").strip() or None,
                )
            )
            existentes.add(clave)
            report.creadas += 1
            report.por_estado[estado.value] = report.por_estado.get(estado.value, 0) + 1

        if dry_run:
            await session.rollback()

    log.info(
        "tasas_importadas",
        archivo=path.name,
        creadas=report.creadas,
        duplicadas=report.duplicadas,
        errores=len(report.errores),
        dry_run=dry_run,
    )
    return report


__all__ = ["TASA_MAXIMA_PLAUSIBLE", "ImportReport", "ImportError_", "import_csv"]
