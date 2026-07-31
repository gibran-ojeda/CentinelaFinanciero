"""Metadatos de la API: frescura de los datos.

§11 obliga a mostrar siempre la fecha de la última actualización de cada dato.
Este endpoint es la versión agregada de esa obligación: en vez de que la UI
tenga que inferir la frescura fila por fila, la API dice explícitamente qué tan
viejo es cada origen y si eso está dentro de lo esperado.

Los SLA no son arbitrarios: reflejan la cadencia real de cada fuente. Banxico
publica diario, la CNBV con uno a tres meses de rezago, y la carga manual del
MVP es semanal. Marcar la CNBV como obsoleta a los dos días sería ruido.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi import APIRouter
from sqlalchemy import func, select

from api.dependencies import LecturaDep, SessionDep
from api.schemas import FrescuraFuente, RespuestaFrescura
from core.config_store import effective
from domain.enums import FuenteTasa
from domain.orm import Tasa

router = APIRouter(prefix="/api/v1/meta", tags=["meta"])

#: Días que puede tener un dato antes de considerarse desactualizado.
#: Días de antigüedad tolerables por fuente. Se recorre este diccionario y no
#: el enum: `AGREGADOR` queda fuera **a propósito**, porque este endpoint mide
#: la frescura de lo que se sirve y un dato de agregador no se sirve nunca.
#: Incluirlo pondría una fila en rojo por datos que no llegan a nadie.
SLA_POR_FUENTE: dict[FuenteTasa, int] = {
    FuenteTasa.BANXICO_API: 2,
    FuenteTasa.CNBV: 100,
    FuenteTasa.FETCH_DIRIGIDO: 10,
    FuenteTasa.LLM_RESEARCH: 10,
    FuenteTasa.MANUAL: 10,
}


@router.get(
    "/frescura",
    response_model=RespuestaFrescura,
    summary="Antigüedad de los datos por fuente",
    responses={401: {"description": "Falta la X-API-Key o no es válida"}},
)
async def frescura(session: SessionDep, _nivel: LecturaDep) -> RespuestaFrescura:
    filas = (
        (
            await session.execute(
                select(
                    Tasa.fuente,
                    func.max(Tasa.fecha_dato),
                    func.count(Tasa.id),
                ).group_by(Tasa.fuente)
            )
        )
        .tuples()
        .all()
    )
    por_fuente = {fuente: (ultima, total) for fuente, ultima, total in filas}
    hoy = date.today()

    fuentes: list[FrescuraFuente] = []
    for fuente, sla in SLA_POR_FUENTE.items():
        ultima, total = por_fuente.get(fuente, (None, 0))
        dias = (hoy - ultima).days if ultima else None
        fuentes.append(
            FrescuraFuente(
                fuente=fuente.value,
                ultima_actualizacion=ultima,
                dias_desde_actualizacion=dias,
                sla_dias=sla,
                # Una fuente sin datos no está fuera de SLA: simplemente no se
                # usa todavía. Marcarla en rojo sería alarma sin causa.
                dentro_de_sla=dias is None or dias <= sla,
                observaciones=total,
            )
        )

    fechas = [f.ultima_actualizacion for f in fuentes if f.ultima_actualizacion]
    return RespuestaFrescura(
        fuentes=fuentes,
        ultima_actualizacion=max(fechas) if fechas else None,
        # Se lee del ConfigStore y no del `ContextoMercado`: éste resuelve
        # parámetros fiscales y devuelve 503 si faltan, lo que haría que una
        # petición sin credenciales recibiera 503 en vez de 401. Este endpoint
        # informa, no calcula.
        modo_demo=effective.mostrar_datos_demo,
        generado_en=datetime.now(UTC),
        todo_dentro_de_sla=all(f.dentro_de_sla for f in fuentes),
    )


__all__ = ["SLA_POR_FUENTE", "router"]
