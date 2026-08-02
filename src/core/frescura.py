"""Cuán viejo es el dato de cada fuente, y si eso está dentro de lo esperado.

§11 obliga a mostrar siempre la fecha de la última actualización. Esto es la
versión agregada de esa obligación, y vive aquí —y no en el router— porque la
consumen dos cosas con propósitos distintos: `GET /api/v1/meta/frescura`, que
la publica, y el job `frescura_check`, que la vigila. Con dos copias, un día el
sitio diría que todo está fresco mientras el job avisa de lo contrario.

**Los SLA no son arbitrarios: son un techo sobre la cadencia real de cada
fuente.** Banxico publica a diario y la CNBV con uno a tres meses de rezago.
El del fetch dirigido quedó holgado a propósito: el job corre cada 4 horas,
pero el SLA vigila que el CICLO funcione (lectura + revisión humana), no que
cada corrida traiga algo. Marcar la CNBV como obsoleta a los dos días sería
ruido, y ruido constante es una alarma que se aprende a ignorar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.enums import FuenteTasa
from domain.orm import IndicadorFinanciero, Tasa

#: Días de antigüedad tolerables por fuente **vigilada**: las que un job
#: alimenta con cadencia conocida. Se recorren estas colecciones y no el enum:
#: `AGREGADOR` queda fuera **a propósito**, porque esto mide la frescura de lo
#: que se afirma y un dato de agregador nunca se afirma — se muestra
#: etiquetado «sin verificar» mientras dura la transición.
SLA_POR_FUENTE: dict[FuenteTasa, int] = {
    FuenteTasa.BANXICO_API: 2,
    FuenteTasa.CNBV: 100,
    FuenteTasa.FETCH_DIRIGIDO: 10,
}

#: Fuentes que se reportan con su fecha y su conteo pero **sin SLA**, porque
#: nada las refresca con cadencia. `MANUAL` son correcciones puntuales de una
#: persona: las filas semilla de CETES no se renuevan porque las supersede
#: `BANXICO_API`, y exigirles diez días habría puesto esa fila en rojo
#: permanente desde agosto de 2026 — la alarma constante que se aprende a
#: ignorar. `LLM_RESEARCH` es descubrimiento oportunista: corre sólo sobre lo
#: que ya está stale, así que su propia antigüedad no mide nada.
FUENTES_INFORMATIVAS: tuple[FuenteTasa, ...] = (FuenteTasa.MANUAL, FuenteTasa.LLM_RESEARCH)


@dataclass(frozen=True, slots=True)
class EstadoFuente:
    """Qué tan vieja está una fuente."""

    fuente: FuenteTasa
    ultima_actualizacion: date | None
    dias: int | None
    #: `None` = fuente informativa: se reporta, no se vigila.
    sla_dias: int | None
    observaciones: int

    @property
    def dentro_de_sla(self) -> bool:
        # Sin SLA no hay retraso posible; y una fuente sin datos no está fuera
        # de SLA: simplemente no se usa todavía. El caso «debería tener datos
        # y no tiene» lo vigila `frescura_check`, que sí sabe qué jobs están
        # encendidos — este módulo no.
        if self.sla_dias is None or self.dias is None:
            return True
        return self.dias <= self.sla_dias


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

    pares: list[tuple[FuenteTasa, int | None]] = [
        *SLA_POR_FUENTE.items(),
        *((fuente, None) for fuente in FUENTES_INFORMATIVAS),
    ]
    estados: list[EstadoFuente] = []
    for fuente, sla in pares:
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


__all__ = ["FUENTES_INFORMATIVAS", "SLA_POR_FUENTE", "EstadoFuente", "evaluar"]
