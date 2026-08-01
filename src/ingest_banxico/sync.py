"""Sincronización incremental de las series del SIE a `valores_serie`.

Por serie: se mira hasta dónde llega lo guardado y se pide sólo lo que falta.
La primera vez no hay nada, así que se cargan tres años — suficientes para los
trece meses de INPC que la inflación anual necesita, y baratos por una vez.

**Idempotente por construcción.** `uq_valor_serie_fecha` impide el duplicado en
la base, pero el duplicado ni se intenta: antes de escribir se leen las fechas
que ya están en el rango y sólo se insertan las que faltan. Correr el job dos
veces el mismo día no escribe nada la segunda.

Una decisión que parece un descuido y no lo es: **el rango pedido termina en el
futuro.** Banxico publica la UDI con diez días de anticipación, así que cortar
en «hoy» dejaría fuera precisamente lo que la serie tiene de más reciente, y a
la corrida siguiente le pasaría lo mismo. Se pide con margen y se guarda tal
cual; quien la consume decide qué valor rige hoy (ver `api.dependencies`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import session_scope
from core.logging import get_logger
from domain.orm import SerieEconomica, ValorSerieEconomica
from ingest_banxico import series as catalogo
from ingest_banxico.client import ClienteSIE, ErrorSIE, ErrorTokenSIE, Observacion

log = get_logger(__name__)

#: Días hacia adelante que se piden. Cubre los diez de adelanto de la UDI con
#: holgura; el SIE devuelve lo que exista y nada más.
MARGEN_FUTURO_DIAS = 30


@dataclass(slots=True)
class ReporteSync:
    """Qué trajo la sincronización. Va a `job_runs.metricas`."""

    series: int = 0
    series_creadas: int = 0
    observaciones: int = 0
    sin_novedad: int = 0
    errores: list[str] = field(default_factory=list)

    def como_metricas(self) -> dict[str, Any]:
        return {
            "series": self.series,
            "series_creadas": self.series_creadas,
            "observaciones": self.observaciones,
            "sin_novedad": self.sin_novedad,
            "errores": self.errores[:20],
        }

    def render(self) -> str:
        lineas = [
            f"  series consultadas      {self.series:>4}",
            f"  series dadas de alta    {self.series_creadas:>4}",
            f"  observaciones nuevas    {self.observaciones:>4}",
            f"  sin novedad             {self.sin_novedad:>4}",
        ]
        for error in self.errores[:10]:
            lineas.append(f"    - {error}")
        return "\n".join(lineas)


async def sincronizar(
    *,
    cliente: ClienteSIE | None = None,
    desde: date | None = None,
    hoy: date | None = None,
) -> ReporteSync:
    """Trae de Banxico lo que falta y lo guarda.

    Args:
        desde: fuerza el inicio del rango para **todas** las series, ignorando
            lo que ya esté guardado. Es lo que usa `cli banxico sync --desde`
            para rellenar un hueco a mano.
    """
    reporte = ReporteSync()
    propio = cliente is None
    cliente = cliente or ClienteSIE()
    hoy = hoy or datetime.now(UTC).date()
    hasta = hoy + timedelta(days=MARGEN_FUTURO_DIAS)

    try:
        async with session_scope() as session:
            filas = await _asegurar_series(session, reporte)
            inicios = await _inicios(session, filas, forzado=desde, hoy=hoy)

        reporte.series = len(filas)

        # Se agrupan las series que arrancan el mismo día para no hacer una
        # petición por cada una. En régimen normal casi todas comparten inicio
        # y esto es una sola llamada; cuando se añade una serie al catálogo,
        # sólo ella carga sus tres años de historia.
        for inicio, claves in sorted(_agrupar(inicios).items()):
            if inicio > hasta:
                reporte.sin_novedad += len(claves)
                continue
            try:
                traidas = await cliente.rango(list(claves), desde=inicio, hasta=hasta)
            except ErrorTokenSIE:
                # Un token rechazado no es un lote que falló: es una credencial
                # caducada, va a fallar con todos los lotes igual, y no se
                # arregla sola. Se propaga para que la corrida quede FALLIDA y
                # alguien la mire, en vez de acumular nueve errores idénticos y
                # terminar «exitosa» sin haber traído nada.
                raise
            except ErrorSIE as exc:
                reporte.errores.append(f"{','.join(sorted(claves))}: {exc}")
                log.warning("sie_lote_fallido", claves=sorted(claves), error=str(exc)[:200])
                continue

            async with session_scope() as session:
                for clave in sorted(claves):
                    nuevas = await _guardar(
                        session,
                        serie_id=filas[clave],
                        clave=clave,
                        observaciones=traidas.get(clave, []),
                    )
                    reporte.observaciones += nuevas
                    if not nuevas:
                        reporte.sin_novedad += 1
    finally:
        if propio:
            await cliente.cerrar()

    log.info("banxico_sync", **{k: v for k, v in reporte.como_metricas().items() if v})
    return reporte


async def _asegurar_series(session: AsyncSession, reporte: ReporteSync) -> dict[str, int]:
    """`{clave: id}` de cada serie del catálogo, creándola si no existía.

    Sólo se crean: una serie que ya está en la base conserva su nombre y su
    descripción. El seed y el catálogo escriben el nombre distinto —«Valor de
    la UDI» contra «Valor de UDIS»— y no hay razón para que el job pise lo que
    alguien pudo ajustar a mano.
    """
    existentes = {
        fila.clave_banxico: fila
        for fila in (await session.execute(select(SerieEconomica))).scalars()
    }
    ids: dict[str, int] = {}
    for serie in catalogo.CATALOGO:
        fila = existentes.get(serie.clave)
        if fila is None:
            fila = SerieEconomica(
                clave_banxico=serie.clave,
                nombre=serie.nombre,
                unidad=serie.unidad,
                descripcion=serie.descripcion,
            )
            session.add(fila)
            await session.flush()
            reporte.series_creadas += 1
            log.info("serie_creada", clave=serie.clave, nombre=serie.nombre)
        ids[serie.clave] = int(fila.id)
    return ids


async def _inicios(
    session: AsyncSession,
    series: dict[str, int],
    *,
    forzado: date | None,
    hoy: date,
) -> dict[str, date]:
    """Desde qué fecha pedir cada serie."""
    if forzado is not None:
        return dict.fromkeys(series, forzado)

    inicial = hoy - timedelta(days=catalogo.DIAS_DE_CARGA_INICIAL)
    # `max` agrupado y no traer las fechas para quedarse con la última: la
    # tabla acumula una fila por día y por serie, y en un año son miles.
    ultimas: dict[int, date] = dict(
        (
            await session.execute(
                select(ValorSerieEconomica.serie_id, func.max(ValorSerieEconomica.fecha))
                .where(ValorSerieEconomica.serie_id.in_(series.values()))
                .group_by(ValorSerieEconomica.serie_id)
            )
        )
        .tuples()
        .all()
    )
    # Se vuelve a pedir el último día guardado en vez de empezar en el
    # siguiente. Cuesta una observación que se descarta y cubre el caso de que
    # Banxico revise el dato más reciente, que con el INPC pasa.
    return {clave: ultimas.get(serie_id, inicial) for clave, serie_id in series.items()}


def _agrupar(inicios: dict[str, date]) -> dict[date, tuple[str, ...]]:
    grupos: dict[date, list[str]] = {}
    for clave, inicio in inicios.items():
        grupos.setdefault(inicio, []).append(clave)
    return {inicio: tuple(sorted(claves)) for inicio, claves in grupos.items()}


async def _guardar(
    session: AsyncSession,
    *,
    serie_id: int,
    clave: str,
    observaciones: list[Observacion],
) -> int:
    """Inserta las observaciones que faltan. Devuelve cuántas eran."""
    if not observaciones:
        return 0

    fechas = {o.fecha for o in observaciones}
    ya_estan = set(
        (
            await session.execute(
                select(ValorSerieEconomica.fecha).where(
                    ValorSerieEconomica.serie_id == serie_id,
                    ValorSerieEconomica.fecha.in_(fechas),
                )
            )
        )
        .scalars()
        .all()
    )

    nuevas = 0
    # Una misma fecha puede llegar repetida en el cuerpo del SIE; el `seen`
    # evita chocar contra la clave única dentro de la propia corrida.
    vistas: set[date] = set()
    for observacion in observaciones:
        if observacion.fecha in ya_estan or observacion.fecha in vistas:
            continue
        vistas.add(observacion.fecha)
        session.add(
            ValorSerieEconomica(
                serie_id=serie_id, fecha=observacion.fecha, valor=observacion.valor
            )
        )
        nuevas += 1

    if nuevas:
        log.info("serie_actualizada", clave=clave, observaciones=nuevas)
    return nuevas


__all__ = ["MARGEN_FUTURO_DIAS", "ReporteSync", "sincronizar"]
