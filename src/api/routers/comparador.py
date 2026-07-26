"""Endpoint del comparador: `GET /api/v1/comparador`.

Todos los filtros de §7. Los parámetros son de tipo enum donde el dominio lo
permite, para que una petición con un valor inventado falle con 422 y un
mensaje útil en vez de devolver una lista vacía que parezca "no hay nada".
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response

from api.dependencies import ContextoDep, LecturaDep, SessionDep
from api.schemas import RespuestaComparador
from api.services import cache
from api.services.comparador import (
    FiltrosComparador,
    OrdenComparador,
    construir_comparador,
)
from domain.enums import CategoriaInstitucion, Liquidez, TipoSeguro

router = APIRouter(prefix="/api/v1", tags=["comparador"])

#: Valores aceptados por el filtro de plazo (§7).
PLAZOS_VALIDOS = {"VISTA", "28", "91", "182", "364", "365+"}


@router.get(
    "/comparador",
    response_model=RespuestaComparador,
    summary="Tabla comparativa de instrumentos de ahorro",
    responses={401: {"description": "Falta la X-API-Key o no es válida"}},
)
async def comparador(
    session: SessionDep,
    contexto: ContextoDep,
    _nivel: LecturaDep,
    plazo: Annotated[
        str | None,
        Query(description="VISTA, 28, 91, 182, 364 o 365+"),
    ] = None,
    categoria: Annotated[
        list[CategoriaInstitucion] | None,
        Query(description="Repetible: `?categoria=SOFIPO&categoria=BANCO_DIGITAL`"),
    ] = None,
    monto: Annotated[
        Decimal | None,
        Query(gt=0, description="Excluye productos con monto mínimo mayor"),
    ] = None,
    seguro: Annotated[
        list[TipoSeguro] | None,
        Query(description="Repetible: `?seguro=IPAB&seguro=PROSOFIPO`. Vacío = todos"),
    ] = None,
    liquidez: Annotated[Liquidez | None, Query()] = None,
    sin_banderas: Annotated[
        bool,
        Query(description="Excluye instituciones con cualquier bandera activa"),
    ] = False,
    orden: Annotated[OrdenComparador, Query()] = OrdenComparador.TEN,
    descendente: Annotated[bool, Query()] = True,
    # `Response` es el camino de cache hit: se devuelve el JSON guardado sin
    # revalidarlo. El `response_model` del decorador mantiene el OpenAPI.
) -> RespuestaComparador | Response:
    if plazo is not None and plazo.upper() not in {p.upper() for p in PLAZOS_VALIDOS}:
        raise HTTPException(
            status_code=422,
            detail=f"Plazo no válido. Valores aceptados: {sorted(PLAZOS_VALIDOS)}",
        )

    filtros = FiltrosComparador(
        plazo=plazo,
        categorias=frozenset(categoria or ()),
        monto=monto,
        seguros=frozenset(seguro or ()),
        liquidez=liquidez,
        sin_banderas=sin_banderas,
        orden=orden,
        descendente=descendente,
    )

    clave = cache.llave(filtros, contexto)
    if (cacheado := await cache.obtener(clave)) is not None:
        # Se devuelve tal cual: revalidarlo contra el esquema costaría casi lo
        # mismo que recalcular, y la llave ya incluye la versión del contrato.
        return Response(content=cacheado, media_type="application/json")

    filas = await construir_comparador(session, contexto, filtros)
    respuesta = RespuestaComparador(
        filas=filas,
        total=len(filas),
        inflacion_anual=contexto.inflacion_anual,
        valor_udi=contexto.valor_udi,
        tasa_retencion_capital=contexto.params_fiscales.tasa_retencion_capital,
        generado_en=datetime.now(UTC),
    )

    await cache.guardar(clave, respuesta.model_dump_json())
    return respuesta


__all__ = ["PLAZOS_VALIDOS", "router"]
