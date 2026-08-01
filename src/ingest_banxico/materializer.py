"""Las subastas de CETES, convertidas en tasas del comparador.

`valores_serie` guarda lo que Banxico publica; `tasas` guarda lo que el
comparador enseña. Este módulo es el puente entre las dos, y sólo para las
cuatro series de subasta: la UDI y el INPC no son tasas de ningún producto, se
consumen como contexto (ver `api.dependencies`).

**Estas tasas se publican VIGENTE sin pasar por revisión.** La cola de revisión
existe porque una extracción con LLM puede equivocarse leyendo una página; aquí
el número viene firmado por quien lo subastó. Es la diferencia entre el nivel 1
y el nivel 2 de §15, y es la razón de que el nivel 1 vaya primero.

Dos cosas que el materializador **no** hace, ambas a propósito:

- **No rellena huecos.** CETES a 364 días no se subasta todas las semanas —en
  julio de 2026 sólo hubo el 9 y el 23—. Arrastrar el valor anterior a las
  semanas sin subasta inventaría observaciones que nadie publicó.
- **No calcula la GAT.** Banxico no publica una GAT para CETES, así que
  `gat_nominal` queda en nulo y el motor de métricas la deriva. Guardar aquí
  una GAT calculada la haría pasar por publicada.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import session_scope
from core.logging import get_logger
from domain.enums import EstadoTasa, FuenteTasa, TipoInstrumento
from domain.orm import Producto, SerieEconomica, Tasa, ValorSerieEconomica
from ingest_banxico import series as catalogo

log = get_logger(__name__)


@dataclass(slots=True)
class ReporteMaterializacion:
    """Qué se llevó de las series a la tabla de tasas."""

    productos: int = 0
    publicadas: int = 0
    ya_estaban: int = 0
    sin_producto: list[str] = field(default_factory=list)
    sin_dato: list[str] = field(default_factory=list)

    def como_metricas(self) -> dict[str, Any]:
        return {
            "productos": self.productos,
            "tasas_publicadas": self.publicadas,
            "tasas_ya_registradas": self.ya_estaban,
            "series_sin_producto": self.sin_producto,
            "series_sin_subasta_nueva": self.sin_dato,
        }

    def render(self) -> str:
        lineas = [
            f"  productos CETES         {self.productos:>4}",
            f"  tasas publicadas        {self.publicadas:>4}",
            f"  ya registradas          {self.ya_estaban:>4}",
        ]
        if self.sin_producto:
            lineas.append(f"  series sin producto     {', '.join(self.sin_producto)}")
        if self.sin_dato:
            # «Nueva» importa: en régimen normal las cuatro salen aquí todos los
            # días de la semana en que no hubo subasta, y sin esa palabra parece
            # que la serie está rota cuando lo que pasa es que no toca.
            lineas.append(f"  sin subasta nueva       {', '.join(self.sin_dato)}")
        return "\n".join(lineas)


async def materializar(*, hoy: date | None = None) -> ReporteMaterializacion:
    """Publica como tasas las subastas que todavía no lo estaban."""
    reporte = ReporteMaterializacion()
    hoy = hoy or datetime.now(UTC).date()

    async with session_scope() as session:
        productos = await _productos_cetes(session)
        for clave, plazo in catalogo.CETES_POR_PLAZO.items():
            producto = productos.get(plazo)
            if producto is None:
                # El catálogo no tiene ese plazo. Se reporta y se sigue: es un
                # hueco de `seeds/productos.yaml`, no un fallo de la ingesta.
                reporte.sin_producto.append(f"{clave} ({plazo} días)")
                log.warning("cetes_sin_producto", clave=clave, plazo_dias=plazo)
                continue

            reporte.productos += 1
            subastas = await _subastas_pendientes(
                session, clave=clave, producto_id=producto.id, hoy=hoy
            )
            if not subastas:
                reporte.sin_dato.append(clave)
                continue

            for fecha, valor in subastas:
                if await _ya_registrada(session, producto_id=producto.id, fecha=fecha):
                    reporte.ya_estaban += 1
                    continue
                session.add(
                    Tasa(
                        producto_id=producto.id,
                        tasa_nominal=valor,
                        fecha_dato=fecha,
                        fuente=FuenteTasa.BANXICO_API,
                        fuente_url=catalogo.URL_PUBLICA_SUBASTA,
                        estado=EstadoTasa.VIGENTE,
                    )
                )
                reporte.publicadas += 1
                log.info(
                    "cetes_publicado",
                    producto=producto.slug,
                    fecha=fecha.isoformat(),
                    tasa=str(valor),
                )

    log.info("banxico_materializar", **{k: v for k, v in reporte.como_metricas().items() if v})
    return reporte


async def _productos_cetes(session: AsyncSession) -> dict[int, Producto]:
    """Productos CETES activos, indexados por plazo.

    Se busca por `instrumento` y no por el nombre de la institución: el
    instrumento es lo que determina el tratamiento fiscal y lo que hace que un
    producto sea CETES, y un nombre se puede reescribir.
    """
    filas = (
        (
            await session.execute(
                select(Producto).where(
                    Producto.instrumento == TipoInstrumento.CETES,
                    Producto.activo.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    return {p.plazo_dias: p for p in filas if p.plazo_dias is not None}


async def _subastas_pendientes(
    session: AsyncSession, *, clave: str, producto_id: int, hoy: date
) -> list[tuple[date, Decimal]]:
    """Observaciones de la serie que aún no están publicadas como tasa.

    Se arranca desde la última `fecha_dato` ya materializada de este producto,
    de modo que una semana en la que el job no corriera se recupera sola en la
    siguiente. La primera vez no hay ninguna y se toma sólo la más reciente:
    republicar tres años de subastas al arrancar llenaría el histórico de
    observaciones que nadie leyó.
    """
    ultima = await session.scalar(
        select(func.max(Tasa.fecha_dato)).where(
            Tasa.producto_id == producto_id,
            Tasa.fuente == FuenteTasa.BANXICO_API,
        )
    )

    consulta = (
        select(ValorSerieEconomica.fecha, ValorSerieEconomica.valor)
        .join(SerieEconomica, SerieEconomica.id == ValorSerieEconomica.serie_id)
        .where(SerieEconomica.clave_banxico == clave, ValorSerieEconomica.fecha <= hoy)
        .order_by(ValorSerieEconomica.fecha.desc())
    )
    if ultima is None:
        consulta = consulta.limit(1)
    else:
        consulta = consulta.where(ValorSerieEconomica.fecha > ultima)

    filas = (await session.execute(consulta)).tuples().all()
    return [(fecha, valor) for fecha, valor in reversed(filas)]


async def _ya_registrada(session: AsyncSession, *, producto_id: int, fecha: date) -> bool:
    """La clave única `(producto, fecha, fuente)` ya la impide; esto la evita.

    Comprobar antes de escribir hace que reintentar el job sea barato en lugar
    de una violación de constraint que tumbaría la corrida entera. Es la misma
    precaución que toma `reviewer.revisar` por la misma razón.
    """
    existente = await session.scalar(
        select(Tasa.id).where(
            Tasa.producto_id == producto_id,
            Tasa.fecha_dato == fecha,
            Tasa.fuente == FuenteTasa.BANXICO_API,
        )
    )
    return existente is not None


__all__ = ["ReporteMaterializacion", "materializar"]
