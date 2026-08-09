"""Calculadora de combinación y optimizador de reparto.

La calculadora de §6 evalúa un producto a la vez. Ésta evalúa un **reparto**:
cuánto rinde y cuánto queda protegido si el monto se divide entre varios
instrumentos. Es la vista principal de la fase 5 después del comparador.

Los dos endpoints devuelven exactamente la misma forma. El de optimización sólo
añade un paso antes: propone los porcentajes en vez de recibirlos, y por eso su
respuesta también incluye las asignaciones — el frontend necesita repoblar el
panel de entrada con lo que se propuso.

Toda respuesta lleva nota fiscal, disclaimer y el aviso de que el optimizador
es una heurística. Van en el esquema y no en la plantilla: quien consuma la API
directamente recibe la misma advertencia que quien mira la página.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter

from api.dependencies import ContextoDep, LecturaDep, SessionDep
from api.schemas import (
    AlternativaSchema,
    AsignacionSchema,
    CascadaSchema,
    DescarteSchema,
    PasoOptimizadorSchema,
    RespuestaCombinacion,
    SolicitudCombinacion,
    SolicitudOptimizador,
    TramoSchema,
)
from api.services.combinacion import Catalogo, alternativas_de, cargar_catalogo, narrativa
from api.services.mappers import (
    bandera_desde_orm,
    cobertura_schema,
    institucion_resumen,
    procedencia,
)
from metrics.portfolio import (
    Asignacion,
    Combinacion,
    Descarte,
    PasoOptimizacion,
    evaluar_combinacion,
    evaluar_reparto,
    optimizar,
)

router = APIRouter(prefix="/api/v1/calculadora", tags=["calculadora"])


def _asignacion_schema(asignacion: Asignacion, catalogo: Catalogo) -> AsignacionSchema:
    producto = catalogo.productos[asignacion.candidato.producto_id]
    return AsignacionSchema(
        institucion=institucion_resumen(producto.institucion),
        producto_id=producto.id,
        producto=producto.nombre,
        producto_slug=producto.slug,
        plazo_dias=producto.plazo_dias,
        porcentaje=asignacion.porcentaje,
        monto=asignacion.monto,
        ten=asignacion.ten,
        cascada=CascadaSchema.model_validate(asignacion.cascada, from_attributes=True),
        escalonada=asignacion.candidato.escalonada,
        tramos=[
            TramoSchema(desde=t.desde, hasta=t.hasta, tasa_nominal=t.tasa_nominal)
            for t in asignacion.candidato.tramos
        ],
        cobertura=cobertura_schema(asignacion.cobertura),
        monto_cubierto=asignacion.monto_cubierto,
        monto_expuesto=asignacion.monto_expuesto,
        cubierto=asignacion.cubierto,
        advertencia_liquidez=asignacion.advertencia_liquidez,
        banderas=[
            bandera_desde_orm(b) for b in catalogo.banderas.get(producto.institucion_id, [])
        ],
        procedencia=procedencia(catalogo.tasas[producto.id]),
    )


def _paso_schema(paso: PasoOptimizacion) -> PasoOptimizadorSchema:
    return PasoOptimizadorSchema(
        producto_id=paso.producto_id,
        indice_tramo=paso.indice_tramo,
        tramo=TramoSchema(
            desde=paso.tramo.desde, hasta=paso.tramo.hasta, tasa_nominal=paso.tramo.tasa_nominal
        ),
        ten_marginal=paso.ten_marginal,
        monto=paso.monto,
        razon_corte=paso.razon_corte,
        compra_minimo=paso.compra_minimo,
    )


def _descarte_schema(descarte: Descarte, catalogo: Catalogo) -> DescarteSchema:
    # Todo descarte proviene de `catalogo.candidatos`, así que su producto
    # siempre está en el índice del catálogo.
    producto = catalogo.productos[descarte.producto_id]
    return DescarteSchema(
        producto_id=descarte.producto_id,
        producto=producto.nombre,
        institucion=producto.institucion.nombre,
        razon=descarte.razon,
    )


def _respuesta(
    combinacion: Combinacion,
    catalogo: Catalogo,
    *,
    inflacion: Decimal,
    valor_udi: Decimal,
    nota_fiscal: str,
    monto_no_asignado: Decimal = Decimal("0.00"),
    pasos: Sequence[PasoOptimizadorSchema] = (),
    descartes: Sequence[DescarteSchema] = (),
    alternativas: Sequence[AlternativaSchema] = (),
) -> RespuestaCombinacion:
    return RespuestaCombinacion(
        monto_total=combinacion.monto_total,
        monto_no_asignado=monto_no_asignado,
        horizonte_dias=combinacion.horizonte_dias,
        ten_ponderada=combinacion.ten_ponderada,
        rendimiento_bruto=combinacion.rendimiento_bruto,
        isr_retenido=combinacion.isr_retenido,
        rendimiento_neto=combinacion.rendimiento_neto,
        efecto_inflacion=combinacion.efecto_inflacion,
        ganancia_real=combinacion.ganancia_real,
        monto_protegido=combinacion.monto_protegido,
        porcentaje_protegido=combinacion.porcentaje_protegido,
        asignaciones=[_asignacion_schema(a, catalogo) for a in combinacion.asignaciones],
        pasos_optimizador=list(pasos),
        descartes_optimizador=list(descartes),
        alternativas=list(alternativas),
        narrativa=narrativa(
            combinacion.rendimiento_bruto,
            combinacion.isr_retenido,
            combinacion.efecto_inflacion,
            combinacion.ganancia_real,
            instrumentos=len(combinacion.asignaciones),
        ),
        nota_fiscal=nota_fiscal,
        inflacion_anual=inflacion,
        valor_udi=valor_udi,
        generado_en=datetime.now(UTC),
    )


def _armar(
    combinacion: Combinacion,
    catalogo: Catalogo,
    contexto: ContextoDep,
    *,
    inflacion: Decimal,
    monto_no_asignado: Decimal = Decimal("0.00"),
    pasos: Sequence[PasoOptimizadorSchema] = (),
    descartes: Sequence[DescarteSchema] = (),
    alternativas: Sequence[AlternativaSchema] = (),
) -> RespuestaCombinacion:
    # La nota fiscal sale de la primera asignación porque el tratamiento es el
    # mismo para todo el catálogo actual (retención sobre capital, §4.2). Si la
    # fase 10 incorpora un instrumento con otra base, aquí habrá que emitir una
    # por tratamiento presente en el reparto, no una sola.
    nota = (
        combinacion.asignaciones[0].cascada.nota_fiscal
        if combinacion.asignaciones
        else contexto.params_fiscales.nota_fiscal
    )
    return _respuesta(
        combinacion,
        catalogo,
        inflacion=inflacion,
        valor_udi=contexto.valor_udi,
        nota_fiscal=nota,
        monto_no_asignado=monto_no_asignado,
        pasos=pasos,
        descartes=descartes,
        alternativas=alternativas,
    )


@router.post(
    "/combinacion",
    response_model=RespuestaCombinacion,
    summary="Rendimiento y protección de un reparto entre varios instrumentos",
    responses={
        401: {"description": "Falta la X-API-Key o no es válida"},
        404: {"description": "Algún producto no existe o no tiene tasa publicable"},
    },
)
async def combinacion(
    solicitud: SolicitudCombinacion,
    session: SessionDep,
    contexto: ContextoDep,
    _nivel: LecturaDep,
) -> RespuestaCombinacion:
    catalogo = await cargar_catalogo(session, contexto)
    candidatos = catalogo.seleccionar([i.producto_id for i in solicitud.items])
    inflacion = (
        solicitud.inflacion_anual
        if solicitud.inflacion_anual is not None
        else contexto.inflacion_anual
    )

    return _armar(
        evaluar_combinacion(
            candidatos,
            [i.porcentaje for i in solicitud.items],
            monto_total=solicitud.monto_total,
            horizonte_dias=solicitud.horizonte_dias,
            inflacion_anual=inflacion,
            params=contexto.params_fiscales,
            valor_udi=contexto.valor_udi,
        ),
        catalogo,
        contexto,
        inflacion=inflacion,
        # `excluir_rojas=True` fijo: esta solicitud no trae el toggle, y una
        # referencia con bandera roja compararía contra lo que el defecto
        # seguro del optimizador excluye.
        alternativas=alternativas_de(
            catalogo,
            monto_total=solicitud.monto_total,
            horizonte_dias=solicitud.horizonte_dias,
            inflacion_anual=inflacion,
            params=contexto.params_fiscales,
            valor_udi=contexto.valor_udi,
            excluir_rojas=True,
        ),
    )


@router.post(
    "/optimizar",
    response_model=RespuestaCombinacion,
    summary="Propone un reparto y lo evalúa",
    responses={401: {"description": "Falta la X-API-Key o no es válida"}},
)
async def optimizador(
    solicitud: SolicitudOptimizador,
    session: SessionDep,
    contexto: ContextoDep,
    _nivel: LecturaDep,
) -> RespuestaCombinacion:
    catalogo = await cargar_catalogo(session, contexto)

    reparto = optimizar(
        catalogo.candidatos,
        monto_total=solicitud.monto_total,
        horizonte_dias=solicitud.horizonte_dias,
        params=contexto.params_fiscales,
        valor_udi=contexto.valor_udi,
        respetar_seguro=solicitud.respetar_seguro,
        excluir_rojas=solicitud.excluir_rojas,
        solo_vista=solicitud.solo_vista,
    )

    inflacion = (
        solicitud.inflacion_anual
        if solicitud.inflacion_anual is not None
        else contexto.inflacion_anual
    )

    # Se evalúa por **importes**, no por los porcentajes que verá el usuario:
    # el optimizador respeta cada tope al centavo y el redondeo a un decimal
    # de porcentaje bastaría para pasarse. Los porcentajes se derivan después,
    # sólo para mostrarlos.
    #
    # Sin nada elegible se devuelve una combinación vacía, no un error: que no
    # haya instrumentos para ese monto y ese horizonte es una respuesta, y la
    # UI la sabe explicar mejor que un 404.
    return _armar(
        evaluar_reparto(
            reparto.candidatos,
            reparto.montos,
            horizonte_dias=solicitud.horizonte_dias,
            inflacion_anual=inflacion,
            params=contexto.params_fiscales,
            valor_udi=contexto.valor_udi,
        ),
        catalogo,
        contexto,
        inflacion=inflacion,
        monto_no_asignado=reparto.monto_no_asignado,
        pasos=[_paso_schema(p) for p in reparto.pasos],
        descartes=[_descarte_schema(d, catalogo) for d in reparto.descartes],
        alternativas=alternativas_de(
            catalogo,
            monto_total=solicitud.monto_total,
            horizonte_dias=solicitud.horizonte_dias,
            inflacion_anual=inflacion,
            params=contexto.params_fiscales,
            valor_udi=contexto.valor_udi,
            excluir_rojas=solicitud.excluir_rojas,
            solo_vista=solicitud.solo_vista,
        ),
    )


__all__ = ["router"]
