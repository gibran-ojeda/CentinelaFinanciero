"""Cuán viejo es el dato de cada fuente, y si eso está dentro de lo esperado.

§11 obliga a mostrar siempre la fecha de la última actualización. Esto es la
versión agregada de esa obligación, y vive aquí —y no en el router— porque la
consumen dos cosas con propósitos distintos: `GET /api/v1/meta/frescura`, que
la publica, y el job `frescura_check`, que la vigila. Con dos copias, un día el
sitio diría que todo está fresco mientras el job avisa de lo contrario.

**Los SLA no son arbitrarios: son la cadencia real de cada fuente.** Banxico
publica a diario, la CNBV con uno a tres meses de rezago, y la lectura de tasas
es semanal. Marcar la CNBV como obsoleta a los dos días sería ruido, y ruido
constante es una alarma que se aprende a ignorar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.enums import FuenteTasa
from domain.orm import IndicadorFinanciero, Tasa

#: Días de antigüedad tolerables por fuente. Se recorre este diccionario y no
#: el enum: `AGREGADOR` queda fuera **a propósito**, porque esto mide la
#: frescura de lo que se sirve y un dato de agregador no se sirve nunca.
#: Incluirlo pondría una fila en rojo por datos que no llegan a nadie.
SLA_POR_FUENTE: dict[FuenteTasa, int] = {
    FuenteTasa.BANXICO_API: 2,
    FuenteTasa.CNBV: 100,
    FuenteTasa.FETCH_DIRIGIDO: 10,
    FuenteTasa.LLM_RESEARCH: 10,
    FuenteTasa.MANUAL: 10,
}


@dataclass(frozen=True, slots=True)
class EstadoFuente:
    """Qué tan vieja está una fuente."""

    fuente: FuenteTasa
    ultima_actualizacion: date | None
    dias: int | None
    sla_dias: int
    observaciones: int

    @property
    def dentro_de_sla(self) -> bool:
        # Una fuente sin datos no está fuera de SLA: simplemente no se usa
        # todavía. Marcarla en rojo sería alarma sin causa.
        return self.dias is None or self.dias <= self.sla_dias


async def evaluar(session: AsyncSession, *, hoy: date | None = None) -> list[EstadoFuente]:
    """Estado de cada fuente, en el orden del SLA.

    `CNBV` se mide contra `indicadores_financieros` y no contra `tasas`: esa
    fuente no publica tasas, publica indicadores de salud. Medirla donde no
    escribe la dejaría eternamente «sin datos».
    """
    hoy = hoy or date.today()

    filas = (
        (
            await session.execute(
                select(Tasa.fuente, func.max(Tasa.fecha_dato), func.count(Tasa.id)).group_by(
                    Tasa.fuente
                )
            )
        )
        .tuples()
        .all()
    )
    por_fuente: dict[FuenteTasa, tuple[date | None, int]] = {
        fuente: (ultima, total) for fuente, ultima, total in filas
    }

    indicadores = (
        (
            await session.execute(
                select(func.max(IndicadorFinanciero.periodo), func.count(IndicadorFinanciero.id))
            )
        )
        .tuples()
        .one()
    )
    por_fuente[FuenteTasa.CNBV] = (indicadores[0], int(indicadores[1] or 0))

    estados: list[EstadoFuente] = []
    for fuente, sla in SLA_POR_FUENTE.items():
        ultima, total = por_fuente.get(fuente, (None, 0))
        estados.append(
            EstadoFuente(
                fuente=fuente,
                ultima_actualizacion=ultima,
                dias=(hoy - ultima).days if ultima else None,
                sla_dias=sla,
                observaciones=total,
            )
        )
    return estados


__all__ = ["SLA_POR_FUENTE", "EstadoFuente", "evaluar"]
