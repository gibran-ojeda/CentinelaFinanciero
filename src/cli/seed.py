"""Carga de catálogos semilla. Idempotente por clave natural.

"Idempotente" aquí significa que correr `python -m cli seed` dos veces deja la
base igual que correrlo una: se hace upsert por la clave natural de cada
entidad, nunca insert a ciegas. Es lo que permite reejecutarlo en cada deploy
sin miedo.

Las tasas **no** se cargan aquí sino con `cli tasas import`, porque son
observaciones append-only y no catálogo: cada corrida añade una fila nueva si
el dato es nuevo, y ninguna modifica una anterior.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import session_scope
from core.logging import get_logger
from domain.enums import (
    CategoriaInstitucion,
    Liquidez,
    NivelCapitalizacion,
    TipoInstrumento,
    TipoProducto,
    TipoSeguro,
)
from domain.orm import (
    FuenteTasas,
    IndicadorFinanciero,
    Institucion,
    ParametroFiscal,
    Producto,
    SerieEconomica,
    ValorSerieEconomica,
)

log = get_logger(__name__)

DEFAULT_SEEDS_DIR = Path(__file__).resolve().parents[2] / "seeds"


class SeedError(Exception):
    """Semilla mal formada o que referencia algo inexistente."""


@dataclass(slots=True)
class SeedReport:
    """Qué hizo la carga. Se imprime al final y lo usan los tests."""

    creados: dict[str, int] = field(default_factory=dict)
    actualizados: dict[str, int] = field(default_factory=dict)
    sin_cambios: dict[str, int] = field(default_factory=dict)

    def registrar(self, entidad: str, accion: str) -> None:
        destino = getattr(self, accion)
        destino[entidad] = destino.get(entidad, 0) + 1

    @property
    def total_creados(self) -> int:
        return sum(self.creados.values())

    @property
    def total_actualizados(self) -> int:
        return sum(self.actualizados.values())

    def render(self) -> str:
        lineas = []
        entidades = sorted({*self.creados, *self.actualizados, *self.sin_cambios})
        for entidad in entidades:
            lineas.append(
                f"  {entidad:24} creados={self.creados.get(entidad, 0):>4} "
                f"actualizados={self.actualizados.get(entidad, 0):>4} "
                f"sin cambios={self.sin_cambios.get(entidad, 0):>4}"
            )
        return "\n".join(lineas) or "  (nada que cargar)"


def _load_yaml(path: Path, clave: str) -> list[dict[str, Any]]:
    if not path.exists():
        log.warning("seed_archivo_ausente", archivo=str(path))
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entradas = data.get(clave) or []
    if not isinstance(entradas, list):
        raise SeedError(f"{path.name}: '{clave}' debe ser una lista")
    return entradas


def _read_csv(path: Path) -> list[dict[str, str]]:
    """Lee un CSV ignorando comentarios y filas vacías."""
    if not path.exists():
        log.warning("seed_archivo_ausente", archivo=str(path))
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        filas = []
        for fila in csv.DictReader(handle):
            primera = next(iter(fila.values()), "") or ""
            if primera.strip().startswith("#") or not primera.strip():
                continue
            filas.append({k: (v or "").strip() for k, v in fila.items() if k})
        return filas


def _decimal(raw: str | float | None) -> Decimal | None:
    if raw is None or raw == "":
        return None
    return Decimal(str(raw))


def _fecha(raw: str | date) -> date:
    if isinstance(raw, date):
        return raw
    return datetime.strptime(raw, "%Y-%m-%d").date()


def _aplicar(instancia: object, campos: dict[str, Any], report: SeedReport, entidad: str) -> None:
    """Copia campos y clasifica el resultado como actualizado o sin cambios."""
    cambios = False
    for campo, valor in campos.items():
        if getattr(instancia, campo) != valor:
            setattr(instancia, campo, valor)
            cambios = True
    report.registrar(entidad, "actualizados" if cambios else "sin_cambios")


# ─── Cargadores por entidad ───────────────────────────────────


async def _seed_instituciones(
    session: AsyncSession, entradas: list[dict[str, Any]], report: SeedReport
) -> dict[str, Institucion]:
    existentes = {
        row.nombre: row for row in (await session.execute(select(Institucion))).scalars()
    }

    for entrada in entradas:
        nombre = entrada["nombre"]
        campos = {
            "slug": entrada["slug"],
            "categoria": CategoriaInstitucion(entrada["categoria"]),
            "tipo_seguro": TipoSeguro(entrada["tipo_seguro"]),
            "nombre_cnbv": entrada.get("nombre_cnbv"),
            "estatus_regulatorio": entrada.get("estatus_regulatorio"),
            "url_sitio": entrada.get("url_sitio"),
            "activa": entrada.get("activa", True),
            "es_demostracion": entrada.get("es_demostracion", False),
            "notas": entrada.get("notas"),
        }
        if (actual := existentes.get(nombre)) is None:
            nueva = Institucion(nombre=nombre, **campos)
            session.add(nueva)
            existentes[nombre] = nueva
            report.registrar("instituciones", "creados")
        else:
            _aplicar(actual, campos, report, "instituciones")

    await session.flush()
    return existentes


async def _seed_productos(
    session: AsyncSession,
    entradas: list[dict[str, Any]],
    instituciones: dict[str, Institucion],
    report: SeedReport,
) -> None:
    existentes = {
        (row.institucion_id, row.nombre, row.plazo_dias): row
        for row in (await session.execute(select(Producto))).scalars()
    }

    for entrada in entradas:
        institucion = instituciones.get(entrada["institucion"])
        if institucion is None:
            raise SeedError(
                f"El producto '{entrada['nombre']}' referencia una institución "
                f"que no está en el catálogo: '{entrada['institucion']}'"
            )
        plazo = entrada.get("plazo_dias")
        clave = (institucion.id, entrada["nombre"], plazo)
        campos = {
            "slug": entrada["slug"],
            "tipo": TipoProducto(entrada["tipo"]),
            "instrumento": TipoInstrumento(entrada["instrumento"]),
            "plazo_dias": plazo,
            "monto_minimo": _decimal(entrada.get("monto_minimo", 0)) or Decimal("0"),
            "liquidez": Liquidez(entrada["liquidez"]),
            "penalizacion_retiro": entrada.get("penalizacion_retiro"),
            "activo": entrada.get("activo", True),
        }
        if (actual := existentes.get(clave)) is None:
            session.add(
                Producto(institucion_id=institucion.id, nombre=entrada["nombre"], **campos)
            )
            report.registrar("productos", "creados")
        else:
            _aplicar(actual, campos, report, "productos")

    await session.flush()


async def _seed_parametros_fiscales(
    session: AsyncSession, entradas: list[dict[str, Any]], report: SeedReport
) -> None:
    existentes = {
        row.anio: row for row in (await session.execute(select(ParametroFiscal))).scalars()
    }

    for entrada in entradas:
        anio = int(entrada["anio"])
        campos = {
            "tasa_retencion_capital": _decimal(entrada["tasa_retencion_capital"]),
            "vigente_desde": _fecha(entrada["vigente_desde"]),
            "fuente_url": entrada.get("fuente_url"),
            "notas": entrada.get("notas"),
        }
        if (actual := existentes.get(anio)) is None:
            session.add(ParametroFiscal(anio=anio, **campos))
            report.registrar("parametros_fiscales", "creados")
        else:
            _aplicar(actual, campos, report, "parametros_fiscales")

    await session.flush()


async def _seed_fuentes_tasas(
    session: AsyncSession,
    entradas: list[dict[str, Any]],
    instituciones: dict[str, Institucion],
    report: SeedReport,
) -> None:
    existentes = {
        (row.institucion_id, row.url): row
        for row in (await session.execute(select(FuenteTasas))).scalars()
    }

    for entrada in entradas:
        institucion = instituciones.get(entrada["institucion"])
        if institucion is None:
            raise SeedError(
                f"La fuente '{entrada['url']}' referencia una institución "
                f"que no está en el catálogo: '{entrada['institucion']}'"
            )
        clave = (institucion.id, entrada["url"])
        campos = {
            "nivel": int(entrada.get("nivel", 2)),
            "requiere_js": bool(entrada.get("requiere_js", False)),
            "activa": entrada.get("activa", True),
        }
        if (actual := existentes.get(clave)) is None:
            session.add(FuenteTasas(institucion_id=institucion.id, url=entrada["url"], **campos))
            report.registrar("fuentes_tasas", "creados")
        else:
            _aplicar(actual, campos, report, "fuentes_tasas")

    await session.flush()


async def _seed_series(
    session: AsyncSession, filas: list[dict[str, str]], report: SeedReport
) -> None:
    series = {
        row.clave_banxico: row for row in (await session.execute(select(SerieEconomica))).scalars()
    }
    valores = {
        (row.serie_id, row.fecha)
        for row in (await session.execute(select(ValorSerieEconomica))).scalars()
    }

    for fila in filas:
        clave = fila["clave_banxico"]
        if (serie := series.get(clave)) is None:
            serie = SerieEconomica(
                clave_banxico=clave,
                nombre=fila["nombre"],
                unidad=fila["unidad"],
            )
            session.add(serie)
            series[clave] = serie
            report.registrar("series_economicas", "creados")
            await session.flush()

        fecha = _fecha(fila["fecha"])
        if (serie.id, fecha) in valores:
            report.registrar("valores_serie", "sin_cambios")
            continue
        valor = _decimal(fila["valor"])
        if valor is None:
            raise SeedError(f"Serie {clave}: valor vacío en {fecha}")
        session.add(ValorSerieEconomica(serie_id=serie.id, fecha=fecha, valor=valor))
        valores.add((serie.id, fecha))
        report.registrar("valores_serie", "creados")

    await session.flush()


async def _seed_indicadores(
    session: AsyncSession,
    filas: list[dict[str, str]],
    instituciones: dict[str, Institucion],
    report: SeedReport,
) -> None:
    existentes = {
        (row.institucion_id, row.periodo): row
        for row in (await session.execute(select(IndicadorFinanciero))).scalars()
    }

    for fila in filas:
        institucion = instituciones.get(fila["institucion"])
        if institucion is None:
            raise SeedError(
                f"El indicador referencia una institución que no está en el "
                f"catálogo: '{fila['institucion']}'"
            )
        periodo = _fecha(fila["periodo"])
        nivel = fila.get("nicap_nivel") or None
        campos = {
            "imor": _decimal(fila.get("imor")),
            "icap": _decimal(fila.get("icap")),
            "icor": _decimal(fila.get("icor")),
            "nicap_nivel": NivelCapitalizacion(nivel) if nivel else None,
            "captacion": _decimal(fila.get("captacion")),
            "cartera_total": _decimal(fila.get("cartera_total")),
            "capital_contable": _decimal(fila.get("capital_contable")),
            "pasivo_total": _decimal(fila.get("pasivo_total")),
            "fuente_url": fila.get("fuente_url") or None,
        }
        if (actual := existentes.get((institucion.id, periodo))) is None:
            session.add(
                IndicadorFinanciero(institucion_id=institucion.id, periodo=periodo, **campos)
            )
            report.registrar("indicadores_financieros", "creados")
        else:
            _aplicar(actual, campos, report, "indicadores_financieros")

    await session.flush()


# ─── Entrada ──────────────────────────────────────────────────


async def run_seed(seeds_dir: Path | None = None) -> SeedReport:
    """Carga todos los catálogos. Una sola transacción: todo o nada."""
    directorio = seeds_dir or DEFAULT_SEEDS_DIR
    report = SeedReport()

    async with session_scope() as session:
        instituciones = await _seed_instituciones(
            session, _load_yaml(directorio / "instituciones.yaml", "instituciones"), report
        )
        await _seed_productos(
            session,
            _load_yaml(directorio / "productos.yaml", "productos"),
            instituciones,
            report,
        )
        await _seed_parametros_fiscales(
            session,
            _load_yaml(directorio / "parametros_fiscales.yaml", "parametros_fiscales"),
            report,
        )
        await _seed_fuentes_tasas(
            session,
            _load_yaml(directorio / "fuentes_tasas.yaml", "fuentes_tasas"),
            instituciones,
            report,
        )
        await _seed_series(session, _read_csv(directorio / "series.csv"), report)
        await _seed_indicadores(
            session, _read_csv(directorio / "indicadores.csv"), instituciones, report
        )

    log.info(
        "seed_completado",
        creados=report.total_creados,
        actualizados=report.total_actualizados,
    )
    return report


__all__ = ["DEFAULT_SEEDS_DIR", "SeedError", "SeedReport", "run_seed"]
