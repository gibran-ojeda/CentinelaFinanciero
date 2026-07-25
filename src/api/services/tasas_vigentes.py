"""Resolución de la tasa a mostrar de cada producto.

`tasas` es append-only: la vigente es la observación más reciente en estado
VIGENTE. Esta consulta la resuelve para muchos productos a la vez, en SQL y no
en Python, porque hacerlo por producto sería una consulta por fila del
comparador.

Por defecto las tasas en `PENDIENTE_REVISION` **no salen por aquí**: es el
punto donde se hace efectiva la promesa de que un dato sin verificar no se
publica. La excepción es el modo demostración (`mostrar_datos_demo`), que las
incluye **marcadas** con `procedencia.verificada = false` para que un entorno
recién levantado no parezca vacío. La fase 6 lo apaga antes de exponer el sitio.

Aun con el modo encendido, una tasa sin verificar nunca desplaza a una
verificada del mismo producto: primero manda el estado y sólo después la fecha.
Sin esa precedencia, una observación pendiente más reciente ocultaría el dato
bueno, que es justo lo contrario de lo que se busca.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.enums import EstadoTasa
from domain.orm import Tasa

#: Estados que pueden llegar a mostrarse, en orden de preferencia.
_PRIORIDAD_ESTADO = case(
    (Tasa.estado == EstadoTasa.VIGENTE, 0),
    else_=1,
)


def _consulta_vigentes(
    producto_ids: Sequence[int] | None = None,
    *,
    incluir_pendientes: bool = False,
) -> Select[tuple[Tasa]]:
    """La observación a mostrar por producto.

    Se usa una función de ventana en vez de un `GROUP BY` con subconsulta
    correlacionada: una sola pasada sobre el índice `ix_tasas_vigentes`.
    """
    estados = [EstadoTasa.VIGENTE]
    if incluir_pendientes:
        estados.append(EstadoTasa.PENDIENTE_REVISION)

    fila = func.row_number().over(
        partition_by=Tasa.producto_id,
        order_by=(_PRIORIDAD_ESTADO, Tasa.fecha_dato.desc(), Tasa.id.desc()),
    )
    base = select(Tasa, fila.label("rn")).where(Tasa.estado.in_(estados))
    if producto_ids is not None:
        base = base.where(Tasa.producto_id.in_(producto_ids))

    sub = base.subquery()
    return select(Tasa).join(sub, Tasa.id == sub.c.id).where(sub.c.rn == 1)


async def tasas_vigentes_por_producto(
    session: AsyncSession,
    producto_ids: Sequence[int] | None = None,
    *,
    incluir_pendientes: bool = False,
) -> dict[int, Tasa]:
    filas = (
        (
            await session.execute(
                _consulta_vigentes(producto_ids, incluir_pendientes=incluir_pendientes)
            )
        )
        .scalars()
        .all()
    )
    return {tasa.producto_id: tasa for tasa in filas}


__all__ = ["tasas_vigentes_por_producto"]
