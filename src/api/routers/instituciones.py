"""Detalle de institución: la capa de profundidad de §11.

La vista principal muestra tasas limpias. Este endpoint entrega **todo** lo que
hay debajo —productos, indicadores de salud, banderas activas e históricas— con
el periodo de cada dato. La UI decide qué mostrar y en qué capa; la API no
recorta por ella.

Que las banderas históricas viajen junto a las activas es deliberado: una
institución que estuvo marcada y dejó de estarlo cuenta una historia distinta a
una que nunca lo estuvo, y el usuario que llega hasta aquí quiere esa historia.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Path, status
from sqlalchemy import desc, select
from sqlalchemy.orm import selectinload

from api.dependencies import ContextoDep, LecturaDep, SessionDep
from api.schemas import (
    DetalleInstitucion,
    IndicadoresSchema,
    IndicadorEvaluadoSchema,
    ProductoDetalle,
)
from api.services.mappers import (
    bandera_desde_orm,
    cobertura_de,
    gat_schema,
    procedencia,
)
from api.services.tasas_vigentes import tasas_vigentes_por_producto
from domain.models import from_orm_indicadores
from domain.orm import Bandera, IndicadorFinanciero, Institucion
from metrics.flags import evaluar_indicadores
from metrics.gat import resolver_gat
from metrics.ten import ten

router = APIRouter(prefix="/api/v1/instituciones", tags=["instituciones"])


@router.get(
    "/{referencia}",
    response_model=DetalleInstitucion,
    summary="Detalle completo de una institución, por id o por slug",
    responses={
        401: {"description": "Falta la X-API-Key o no es válida"},
        404: {"description": "No existe una institución con esa referencia"},
    },
)
async def detalle(
    session: SessionDep,
    contexto: ContextoDep,
    _nivel: LecturaDep,
    referencia: str = Path(
        min_length=1,
        description=(
            "Id numérico o slug. El frontend usa el slug porque su URL "
            "(`/institucion/nu-mexico`) tiene que ser legible e indexable; el "
            "resto de la API sigue trabajando con ids."
        ),
    ),
) -> DetalleInstitucion:
    # Todo dígito es un id; lo demás, un slug. Los slugs se generan a partir
    # del nombre y ninguno puede quedar en sólo dígitos, así que la regla no
    # es ambigua.
    criterio = (
        Institucion.id == int(referencia)
        if referencia.isdigit()
        else Institucion.slug == referencia
    )
    # La ficha comparte la invariante del comparador: una institución de
    # demostración jamás se sirve. Era la fuga que la auditoría encontró —
    # este endpoint devolvía la ficha completa de las ficticias con la
    # bandera vieja apagada.
    institucion = await session.scalar(
        select(Institucion)
        .options(selectinload(Institucion.productos))
        .where(criterio, Institucion.es_demostracion.is_(False))
    )
    if institucion is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No existe la institución '{referencia}'",
        )
    institucion_id = institucion.id

    productos = sorted(
        institucion.productos, key=lambda p: (p.tipo.value, p.plazo_dias or 0, p.nombre)
    )
    vigentes = await tasas_vigentes_por_producto(
        session, [p.id for p in productos], incluir_pendientes=contexto.incluir_sin_verificar
    )

    detalles: list[ProductoDetalle] = []
    for producto in productos:
        tasa = vigentes.get(producto.id)
        gat = (
            resolver_gat(
                tasa.tasa_nominal,
                producto.instrumento,
                contexto.inflacion_anual,
                contexto.params_fiscales,
                gat_publicada_nominal=tasa.gat_nominal,
                gat_publicada_real=tasa.gat_real,
            )
            if tasa
            else None
        )
        detalles.append(
            ProductoDetalle(
                id=producto.id,
                nombre=producto.nombre,
                slug=producto.slug,
                tipo=producto.tipo,
                instrumento=producto.instrumento,
                plazo_dias=producto.plazo_dias,
                monto_minimo=producto.monto_minimo,
                liquidez=producto.liquidez,
                penalizacion_retiro=producto.penalizacion_retiro,
                tasa_nominal=tasa.tasa_nominal if tasa else None,
                ten=(
                    ten(tasa.tasa_nominal, producto.instrumento, contexto.params_fiscales)
                    if tasa
                    else None
                ),
                gat=gat_schema(gat) if gat else None,
                procedencia=procedencia(tasa) if tasa else None,
            )
        )

    ultimo = await session.scalar(
        select(IndicadorFinanciero)
        .where(IndicadorFinanciero.institucion_id == institucion_id)
        .order_by(desc(IndicadorFinanciero.periodo))
        .limit(1)
    )

    banderas = (
        (
            await session.execute(
                select(Bandera)
                .where(Bandera.institucion_id == institucion_id)
                .order_by(desc(Bandera.created_at))
            )
        )
        .scalars()
        .all()
    )

    indicadores: IndicadoresSchema | None = None
    if ultimo is not None:
        indicadores = IndicadoresSchema.model_validate(ultimo)
        indicadores = indicadores.model_copy(
            update={
                "evaluados": [
                    IndicadorEvaluadoSchema.model_validate(e, from_attributes=True)
                    for e in evaluar_indicadores(from_orm_indicadores(ultimo), contexto.umbrales)
                ]
            }
        )

    return DetalleInstitucion(
        id=institucion.id,
        nombre=institucion.nombre,
        slug=institucion.slug,
        categoria=institucion.categoria,
        tipo_seguro=institucion.tipo_seguro,
        estatus_regulatorio=institucion.estatus_regulatorio,
        url_sitio=institucion.url_sitio,
        activa=institucion.activa,
        es_demostracion=institucion.es_demostracion,
        notas=institucion.notas,
        cobertura=cobertura_de(institucion, contexto.valor_udi),
        productos=detalles,
        indicadores_ultimo_periodo=indicadores,
        banderas_activas=[bandera_desde_orm(b) for b in banderas if b.activa],
        banderas_historicas=[bandera_desde_orm(b) for b in banderas if not b.activa],
    )


__all__ = ["router"]
