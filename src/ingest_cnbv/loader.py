"""De los boletines a `indicadores_financieros`, y de ahí a las banderas.

Es la corrida completa de la fase 8: descubrir qué publicó la CNBV, bajarlo,
leerlo, casarlo con el catálogo, guardarlo y recomputar las banderas. Una sola
función para el job mensual y para la CLI, por la misma razón que en la fase 7:
dos caminos divergen.

**Cada fuente trae su propio periodo y eso no es un descuido.** El boletín de
banca múltiple es mensual y el de SOFIPOs trimestral, así que en julio de 2026
el último de banca era de mayo y el de SOFIPOs de marzo. `periodo` se guarda
por fila con el del boletín del que salió, que es lo que §15 obliga a enseñar:
«cifras a marzo de 2026» y no «cifras de hoy».

**Se salta lo que ya está.** Si el periodo más reciente que publica la CNBV ya
está cargado, no se descarga nada. La CNBV publica con uno a tres meses de
rezago y sin fecha fija, así que el job corre a diario durante su ventana y la
mayoría de los días no hay nada nuevo — bajarse dos megas para descubrirlo
sería tirar ancho de banda de un portal público.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import session_scope
from core.logging import get_logger
from core.settings import settings
from domain.enums import NivelCapitalizacion
from domain.orm import IndicadorFinanciero, Institucion
from ingest_cnbv import fuentes
from ingest_cnbv.downloader import BoletinNoPublicado, DescargadorCNBV
from ingest_cnbv.nicap import leer_nicap
from ingest_cnbv.normalizer import Candidata, ReporteMapeo, clave, mapear
from ingest_cnbv.parser import FilaInstitucion, FormatoInesperado, combinar, leer_hoja

log = get_logger(__name__)


@dataclass(slots=True)
class ReporteFuente:
    """Qué pasó con una de las publicaciones."""

    clave: str
    archivo: str | None = None
    periodo: date | None = None
    url: str | None = None
    omitida: str | None = None
    creados: int = 0
    actualizados: int = 0
    mapeo: ReporteMapeo | None = None
    error: str | None = None

    def como_metricas(self) -> dict[str, Any]:
        datos: dict[str, Any] = {
            "archivo": self.archivo,
            "periodo": self.periodo.isoformat() if self.periodo else None,
            "creados": self.creados,
            "actualizados": self.actualizados,
        }
        if self.omitida:
            datos["omitida"] = self.omitida
        if self.error:
            datos["error"] = self.error
        if self.mapeo is not None:
            datos.update(self.mapeo.como_metricas())
        return datos


@dataclass(slots=True)
class ReporteCarga:
    """La corrida entera."""

    fuentes: list[ReporteFuente] = field(default_factory=list)
    banderas: dict[str, int] = field(default_factory=dict)

    @property
    def hubo_cambios(self) -> bool:
        return any(f.creados or f.actualizados for f in self.fuentes)

    @property
    def hubo_errores(self) -> bool:
        return any(f.error for f in self.fuentes)

    def como_metricas(self) -> dict[str, Any]:
        return {
            "fuentes": {f.clave: f.como_metricas() for f in self.fuentes},
            "banderas": self.banderas,
        }

    def render(self) -> str:
        lineas: list[str] = []
        for fuente in self.fuentes:
            lineas.append(f"  {fuente.clave}")
            if fuente.omitida:
                lineas.append(f"    omitida: {fuente.omitida}")
                continue
            if fuente.error:
                lineas.append(f"    error: {fuente.error}")
                continue
            lineas.append(f"    archivo               {fuente.archivo}")
            lineas.append(f"    periodo               {fuente.periodo}")
            lineas.append(f"    indicadores creados   {fuente.creados}")
            lineas.append(f"    actualizados          {fuente.actualizados}")
            if fuente.mapeo is not None:
                lineas.append(fuente.mapeo.render().replace("  ", "    ", 1))
        if self.banderas:
            resumen = ", ".join(f"{k}={v}" for k, v in self.banderas.items())
            lineas.append(f"  banderas              {resumen}")
        return "\n".join(lineas)


async def cargar(
    *,
    descargador: DescargadorCNBV | None = None,
    directorio: Path | None = None,
    forzar: bool = False,
    recomputar_banderas: bool = True,
) -> ReporteCarga:
    """Descubre, descarga, carga y recomputa. Devuelve qué pasó con cada fuente.

    Args:
        forzar: vuelve a cargar el último periodo aunque ya esté. Sirve para
            reprocesar tras corregir un mapeo, no para el job.
    """
    reporte = ReporteCarga()
    propio = descargador is None
    descargador = descargador or DescargadorCNBV()
    destino = directorio or Path(settings.cnbv_directorio_descargas)

    try:
        for fuente in fuentes.FUENTES:
            reporte.fuentes.append(
                await _procesar(descargador, fuente, destino=destino, forzar=forzar)
            )
    finally:
        if propio:
            await descargador.cerrar()

    if recomputar_banderas and reporte.hubo_cambios:
        # Encadenado: las banderas dejan de venir del seed y pasan a moverse
        # con el dato regulatorio real. `recomputar` es idempotente, así que
        # llamarlo de más no acumula nada.
        from scheduler.jobs.banderas import recomputar

        reporte.banderas = await recomputar()

    log.info("cnbv_carga", **reporte.como_metricas())
    return reporte


async def _procesar(
    descargador: DescargadorCNBV,
    fuente: fuentes.Fuente,
    *,
    destino: Path,
    forzar: bool,
) -> ReporteFuente:
    resultado = ReporteFuente(clave=fuente.clave)
    try:
        publicacion = await descargador.ultimo(
            sector=fuente.sector, tema=fuente.tema, extension=fuente.extension
        )
    except BoletinNoPublicado as exc:
        # No es un fallo: la CNBV publica con rezago. El job reintenta mañana.
        resultado.omitida = str(exc)
        return resultado

    resultado.archivo = publicacion.archivo
    resultado.periodo = publicacion.periodo
    resultado.url = publicacion.url

    if not forzar and await _ya_cargado(fuente, publicacion.periodo):
        resultado.omitida = f"el periodo {publicacion.periodo} ya está cargado"
        return resultado

    try:
        ruta = await descargador.descargar(publicacion, destino / publicacion.archivo)
        filas = _leer(fuente, ruta, publicacion.periodo)
    except FormatoInesperado as exc:
        # Un cambio de formato **rompe la fuente y no carga nada** (§8). Se
        # anota y se sigue con las otras: que la CNBV cambie el boletín de
        # banca no tiene por qué impedir cargar el de SOFIPOs.
        resultado.error = str(exc)
        log.error("cnbv_formato_inesperado", fuente=fuente.clave, error=str(exc))
        return resultado

    async with session_scope() as session:
        candidatas = await _candidatas(session)
        resultado.mapeo = mapear(candidatas, filas, categorias=fuente.categorias)
        for institucion_id, fila in resultado.mapeo.casadas.items():
            creado = await _upsert(
                session,
                institucion_id=institucion_id,
                periodo=publicacion.periodo,
                fila=fila,
                fuente_url=publicacion.url,
            )
            if creado:
                resultado.creados += 1
            else:
                resultado.actualizados += 1

    return resultado


def _leer(fuente: fuentes.Fuente, ruta: Path, periodo: date) -> dict[str, FilaInstitucion]:
    """Cada fuente sabe qué hojas —o qué PDF— hay que leerle."""
    if fuente is fuentes.NCYAT_SOFIPO:
        return {
            nivel.nombre_cnbv: FilaInstitucion(
                nombre_cnbv=nivel.nombre_cnbv,
                valores={"nicap_nivel": nivel.nivel},
            )
            for nivel in leer_nicap(ruta, periodo=periodo)
        }

    hojas = fuentes.HOJAS_BANCA if fuente is fuentes.BOLETIN_BANCA else fuentes.HOJAS_SOFIPO
    return combinar(*[leer_hoja(ruta, hoja, periodo=periodo) for hoja in hojas])


async def _ya_cargado(fuente: fuentes.Fuente, periodo: date) -> bool:
    """¿Esta fuente ya cargó ese periodo?

    Se pregunta por **su columna testigo** y no sólo por el periodo, por dos
    razones que costaron un test cada una:

    - El boletín de SOFIPOs y el PDF de NICAP son la misma figura y pueden
      caer en el mismo mes. Preguntar sólo por el periodo haría que el segundo
      se saltara por el trabajo del primero, y el NICAP no llegaría nunca.
    - Las instituciones ilustrativas del seed traen indicadores sembrados con
      un periodo fijo. Si coincide con el del boletín —y coincidía—, la fuente
      se salta entera creyendo que ya corrió. Por eso se excluyen: la ingesta
      no las escribe, así que tampoco puede leerlas como señal.
    """
    columna = getattr(IndicadorFinanciero, fuente.columna_testigo)
    async with session_scope() as session:
        existente = await session.scalar(
            select(IndicadorFinanciero.id)
            .join(Institucion, Institucion.id == IndicadorFinanciero.institucion_id)
            .where(
                IndicadorFinanciero.periodo == periodo,
                columna.is_not(None),
                Institucion.es_demostracion.is_(False),
            )
            .limit(1)
        )
    return existente is not None


async def _candidatas(session: AsyncSession) -> list[Candidata]:
    """Todo el catálogo activo y no ilustrativo.

    Se cruza **todo** el catálogo contra cada boletín en vez de filtrar por
    figura: Nu México está aquí como banco digital y la CNBV lo publica entre
    las SOFIPOs. Filtrar por categoría lo dejaría sin datos por una
    discrepancia de clasificación que no le toca resolver a la ingesta — la
    categoría sigue mandando en la cobertura de seguro, que es otra cosa.

    Las ilustrativas quedan fuera porque no existen: no están en ningún
    boletín y sus indicadores los pone el seed.
    """
    filas = (
        (
            await session.execute(
                select(Institucion).where(
                    Institucion.activa.is_(True),
                    Institucion.es_demostracion.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        Candidata(id=f.id, nombre=f.nombre, nombre_cnbv=f.nombre_cnbv, categoria=f.categoria)
        for f in filas
    ]


async def _upsert(
    session: AsyncSession,
    *,
    institucion_id: int,
    periodo: date,
    fila: FilaInstitucion,
    fuente_url: str,
) -> bool:
    """Crea o actualiza la fila de ese periodo. Devuelve si fue alta.

    `uq_indicador_periodo` ya impide el duplicado; esto lo evita, que es lo que
    permite reprocesar un periodo sin que reviente la corrida. Y **no se pisa
    un dato con un hueco**: las tres fuentes escriben sobre el mismo periodo
    —el NICAP viene de un PDF distinto al del IMOR— y la segunda no puede
    borrar lo que trajo la primera.
    """
    existente = await session.scalar(
        select(IndicadorFinanciero).where(
            IndicadorFinanciero.institucion_id == institucion_id,
            IndicadorFinanciero.periodo == periodo,
        )
    )
    creado = existente is None
    if existente is None:
        existente = IndicadorFinanciero(institucion_id=institucion_id, periodo=periodo)
        session.add(existente)

    for campo, valor in _campos(fila).items():
        if valor is not None:
            setattr(existente, campo, valor)
    existente.fuente_url = fuente_url
    await session.flush()
    return creado


def _campos(fila: FilaInstitucion) -> dict[str, Any]:
    """De lo leído a las columnas de `indicadores_financieros`."""
    cartera = fila.numero("cartera_total")
    if cartera is None:
        # SOFIPOs publican la cartera partida por etapa; la total es la suma.
        vigente = fila.numero("cartera_vigente")
        etapa3 = fila.numero("cartera_etapa3")
        cartera = (vigente or Decimal(0)) + (etapa3 or Decimal(0)) if vigente or etapa3 else None

    return {
        "imor": fila.numero("imor"),
        "icap": fila.numero("icap"),
        "icor": fila.numero("icor"),
        "captacion": fila.numero("captacion"),
        "cartera_total": cartera,
        "nicap_nivel": _nivel(fila.texto("nicap_nivel")),
    }


def _nivel(crudo: str | None) -> NivelCapitalizacion | None:
    if not crudo:
        return None
    try:
        return NivelCapitalizacion(crudo)
    except ValueError:
        log.warning("cnbv_nicap_desconocido", valor=crudo)
        return None


__all__ = ["ReporteCarga", "ReporteFuente", "cargar", "clave"]
