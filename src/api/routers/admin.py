"""Endpoints de administración. Requieren la llave de admin.

Cubren dos cosas: alta manual de tasas —la vía de corrección cuando una
ingesta se equivoca— y la cola de revisión humana de §15.

La cola se llena en la fase 9, cuando el agente LLM empiece a extraer tasas:
toda observación fuera de tolerancia entra aquí y **no se publica** hasta que
alguien la apruebe. El contrato queda listo ahora para que el flujo de
aprobación exista antes que el generador de trabajo, y no al revés.

Aprobar una revisión publica la tasa; rechazarla la marca como RECHAZADA. En
ninguno de los dos casos se borra nada: la tabla es append-only y la decisión
queda registrada con quién la tomó.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Path
from sqlalchemy import select

from api.dependencies import AdminDep, SessionDep
from api.schemas import AltaTasa, ResolucionRevision, RevisionPendiente, TasaCreada
from api.services import cache, revisiones
from core.logging import get_logger
from domain.enums import EstadoRevision, EstadoTasa
from domain.orm import Institucion, Producto, Tasa

log = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post(
    "/tasas",
    response_model=TasaCreada,
    status_code=201,
    summary="Alta manual de una tasa",
    responses={
        401: {"description": "Falta la X-API-Key o no es válida"},
        403: {"description": "La llave de lectura no puede escribir"},
        404: {"description": "El producto no existe"},
        409: {"description": "Ya existe esa observación para el producto y la fuente"},
    },
)
async def alta_tasa(
    alta: AltaTasa,
    session: SessionDep,
    _actor: AdminDep,
) -> TasaCreada:
    producto = await session.get(Producto, alta.producto_id)
    if producto is None:
        raise HTTPException(status_code=404, detail=f"No existe el producto {alta.producto_id}")

    if alta.fecha_dato > date.today():
        raise HTTPException(
            status_code=422,
            detail="La fecha del dato no puede estar en el futuro",
        )

    duplicada = await session.scalar(
        select(Tasa).where(
            Tasa.producto_id == alta.producto_id,
            Tasa.fecha_dato == alta.fecha_dato,
            Tasa.fuente == alta.fuente,
        )
    )
    if duplicada is not None:
        # La tabla es append-only: reintentar no debe crear una segunda
        # observación del mismo hecho ni sobrescribir la anterior.
        raise HTTPException(
            status_code=409,
            detail=(
                f"Ya existe una observación de {alta.fuente.value} para ese producto "
                f"con fecha {alta.fecha_dato.isoformat()} (tasa {duplicada.id})"
            ),
        )

    tasa = Tasa(
        producto_id=alta.producto_id,
        tasa_nominal=alta.tasa_nominal,
        gat_nominal=alta.gat_nominal,
        gat_real=alta.gat_real,
        fecha_dato=alta.fecha_dato,
        fuente=alta.fuente,
        fuente_url=alta.fuente_url,
        estado=EstadoTasa.VIGENTE,
        notas=alta.notas,
    )
    session.add(tasa)
    await session.commit()
    await session.refresh(tasa)

    await cache.invalidar()
    log.info("tasa_alta_manual", tasa_id=tasa.id, producto_id=alta.producto_id)

    return TasaCreada(
        id=tasa.id,
        producto_id=tasa.producto_id,
        tasa_nominal=tasa.tasa_nominal,
        fecha_dato=tasa.fecha_dato,
        estado=tasa.estado.value,
    )


@router.get(
    "/revisiones",
    response_model=list[RevisionPendiente],
    summary="Cola de revisión de tasas",
    responses={401: {"description": "Falta la X-API-Key o no es válida"}},
)
async def listar_revisiones(
    session: SessionDep,
    _actor: AdminDep,
    estado: EstadoRevision = EstadoRevision.PENDIENTE,
) -> list[RevisionPendiente]:
    # La consulta vive en `services.revisiones` porque la CLI del operador usa
    # exactamente la misma cola: dos implementaciones acabarían discrepando, y
    # la que se usa a diario es la otra.
    return [
        RevisionPendiente(
            id=fila.id,
            tasa_id=fila.tasa_id,
            producto=fila.producto,
            institucion=fila.institucion,
            motivo=fila.motivo,
            valor_anterior=fila.valor_anterior,
            valor_nuevo=fila.valor_nuevo,
            estado=fila.estado.value,
            created_at=fila.creada_en,
        )
        for fila in await revisiones.listar(session, estado=estado)
    ]


@router.post(
    "/revisiones/{revision_id}",
    response_model=RevisionPendiente,
    summary="Aprobar o rechazar una revisión",
    responses={
        401: {"description": "Falta la X-API-Key o no es válida"},
        404: {"description": "No existe esa revisión"},
        409: {"description": "La revisión ya estaba resuelta"},
    },
)
async def resolver_revision(
    resolucion: ResolucionRevision,
    session: SessionDep,
    _actor: AdminDep,
    revision_id: int = Path(gt=0),
) -> RevisionPendiente:
    try:
        revision = await revisiones.resolver(
            session,
            revision_id,
            aprobar=resolucion.aprobar,
            revisor=resolucion.revisor,
            comentario=resolucion.comentario,
        )
    except revisiones.RevisionNoEncontrada as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except revisiones.RevisionYaResuelta as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    await session.commit()
    await session.refresh(revision)

    if resolucion.aprobar:
        await cache.invalidar()

    tasa = await session.get(Tasa, revision.tasa_id)
    assert tasa is not None  # `resolver` ya la habría rechazado si no existiera

    nombres = (
        await session.execute(
            select(Producto.nombre, Institucion.nombre)
            .join(Institucion, Institucion.id == Producto.institucion_id)
            .where(Producto.id == tasa.producto_id)
        )
    ).one()

    return RevisionPendiente(
        id=revision.id,
        tasa_id=revision.tasa_id,
        producto=nombres[0],
        institucion=nombres[1],
        motivo=revision.motivo,
        valor_anterior=revision.valor_anterior,
        valor_nuevo=revision.valor_nuevo,
        estado=revision.estado.value,
        created_at=revision.created_at,
    )


__all__ = ["router"]
