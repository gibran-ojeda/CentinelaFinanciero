"""Resolución de la tasa vigente de cada producto.

`tasas` es append-only: la vigente es la observación más reciente en estado
VIGENTE. Esta consulta la resuelve para muchos productos a la vez, en SQL y no
en Python, porque hacerlo por producto sería una consulta por fila del
comparador.

Las tasas en `PENDIENTE_REVISION` **nunca** salen por aquí. Es el punto donde
se hace efectiva la promesa de que un dato sin verificar no se publica.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.enums import EstadoTasa
from domain.orm import Tasa


def _consulta_vigentes(producto_ids: Sequence[int] | None = None) -> Select[tuple[Tasa]]:
    """La observación más reciente en estado VIGENTE, por producto.

    Se usa una función de ventana en vez de un `GROUP BY` con subconsulta
    correlacionada: una sola pasada sobre el índice `ix_tasas_vigentes`.
    """
    fila = func.row_number().over(
        partition_by=Tasa.producto_id,
        order_by=(Tasa.fecha_dato.desc(), Tasa.id.desc()),
    )
    base = select(Tasa, fila.label("rn")).where(Tasa.estado == EstadoTasa.VIGENTE)
    if producto_ids is not None:
        base = base.where(Tasa.producto_id.in_(producto_ids))

    sub = base.subquery()
    return select(Tasa).join(sub, Tasa.id == sub.c.id).where(sub.c.rn == 1)


async def tasas_vigentes_por_producto(
    session: AsyncSession, producto_ids: Sequence[int] | None = None
) -> dict[int, Tasa]:
    filas = (await session.execute(_consulta_vigentes(producto_ids))).scalars().all()
    return {tasa.producto_id: tasa for tasa in filas}


__all__ = ["tasas_vigentes_por_producto"]
