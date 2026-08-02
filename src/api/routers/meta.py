"""Metadatos de la API: frescura de los datos.

§11 obliga a mostrar siempre la fecha de la última actualización de cada dato.
Este endpoint es la versión agregada de esa obligación: en vez de que la UI
tenga que inferir la frescura fila por fila, la API dice explícitamente qué tan
viejo es cada origen y si eso está dentro de lo esperado.

El cálculo vive en `core.frescura` porque el job `frescura_check` lo vigila con
los mismos SLA. Con dos copias, un día el sitio diría que todo está fresco
mientras el job avisa de lo contrario.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from api.dependencies import LecturaDep, SessionDep
from api.schemas import FrescuraFuente, RespuestaFrescura
from core.config_store import effective
from core.frescura import SLA_POR_FUENTE, evaluar

router = APIRouter(prefix="/api/v1/meta", tags=["meta"])


@router.get(
    "/frescura",
    response_model=RespuestaFrescura,
    summary="Antigüedad de los datos por fuente",
    responses={401: {"description": "Falta la X-API-Key o no es válida"}},
)
async def frescura(session: SessionDep, _nivel: LecturaDep) -> RespuestaFrescura:
    fuentes = [
        FrescuraFuente(
            fuente=estado.fuente.value,
            ultima_actualizacion=estado.ultima_actualizacion,
            dias_desde_actualizacion=estado.dias,
            sla_dias=estado.sla_dias,
            dentro_de_sla=estado.dentro_de_sla,
            observaciones=estado.observaciones,
        )
        for estado in await evaluar(session)
    ]

    fechas = [f.ultima_actualizacion for f in fuentes if f.ultima_actualizacion]
    return RespuestaFrescura(
        fuentes=fuentes,
        ultima_actualizacion=max(fechas) if fechas else None,
        # Se lee del ConfigStore y no del `ContextoMercado`: éste resuelve
        # parámetros fiscales y devuelve 503 si faltan, lo que haría que una
        # petición sin credenciales recibiera 503 en vez de 401. Este endpoint
        # informa, no calcula.
        mostrar_tasas_sin_verificar=effective.mostrar_tasas_sin_verificar,
        generado_en=datetime.now(UTC),
        todo_dentro_de_sla=all(f.dentro_de_sla for f in fuentes),
    )


__all__ = ["SLA_POR_FUENTE", "router"]
