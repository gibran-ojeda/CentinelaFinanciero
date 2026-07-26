"""Calculadora de rendimiento real (§6 del foundation).

No es un extra: es una de las herramientas centrales del producto. Muestra los
cinco conceptos en cascada —bruto, ISR, neto, inflación, real— para que el
usuario vea de dónde sale la diferencia entre la tasa que le anuncian y lo que
acaba ganando.

Toda respuesta lleva **nota fiscal y disclaimer**. La nota dice qué retención
se aplicó y desde cuándo (§6); el disclaimer, que esto no es asesoría (§19).
Ninguno es opcional: van en el esquema, no en la plantilla del frontend.

Además de la cascada, cada resultado incluye la cobertura de seguro y el monto
que quedaría expuesto. Un cálculo que sólo mostrara el rendimiento invitaría a
comparar por tasa e ignorar cuánto del dinero está protegido, que es justo lo
que §4.6 dice que no debe quedar en letra chica.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from api.dependencies import ContextoDep, LecturaDep, SessionDep
from api.schemas import (
    CascadaSchema,
    RespuestaCalculadora,
    ResultadoCalculadora,
    SolicitudCalculadora,
)
from api.services.mappers import (
    bandera_desde_orm,
    cobertura_de,
    institucion_resumen,
    procedencia,
)
from api.services.tasas_vigentes import tasas_vigentes_por_producto
from domain.models import from_orm_producto
from domain.orm import Bandera, Producto
from metrics.coverage import resolver_cobertura
from metrics.real import desglose_cascada

router = APIRouter(prefix="/api/v1", tags=["calculadora"])


@router.post(
    "/calculadora",
    response_model=RespuestaCalculadora,
    summary="Desglose de ganancia real por producto",
    responses={
        401: {"description": "Falta la X-API-Key o no es válida"},
        404: {"description": "Algún producto no existe o no tiene tasa publicable"},
    },
)
async def calcular(
    solicitud: SolicitudCalculadora,
    session: SessionDep,
    contexto: ContextoDep,
    _nivel: LecturaDep,
) -> RespuestaCalculadora:
    productos = (
        (
            await session.execute(
                select(Producto)
                .options(selectinload(Producto.institucion))
                .where(Producto.id.in_(solicitud.producto_ids))
            )
        )
        .scalars()
        .all()
    )

    faltantes = set(solicitud.producto_ids) - {p.id for p in productos}
    if faltantes:
        raise HTTPException(status_code=404, detail=f"Productos inexistentes: {sorted(faltantes)}")

    vigentes = await tasas_vigentes_por_producto(session, [p.id for p in productos])
    sin_tasa = [p.slug for p in productos if p.id not in vigentes]
    if sin_tasa:
        # Se falla en vez de omitirlos: una calculadora que devuelve menos
        # resultados de los pedidos, en silencio, hace comparar peras con nada.
        raise HTTPException(
            status_code=404,
            detail=(
                f"Sin tasa publicable para: {sorted(sin_tasa)}. "
                f"Puede que su tasa esté pendiente de verificación."
            ),
        )

    inflacion = (
        solicitud.inflacion_anual
        if solicitud.inflacion_anual is not None
        else contexto.inflacion_anual
    )

    banderas: dict[int, list[Bandera]] = {}
    for bandera in (
        (
            await session.execute(
                select(Bandera).where(
                    Bandera.activa.is_(True),
                    Bandera.institucion_id.in_({p.institucion_id for p in productos}),
                )
            )
        )
        .scalars()
        .all()
    ):
        banderas.setdefault(bandera.institucion_id, []).append(bandera)

    # Se respeta el orden en que el usuario pidió los productos: es el orden en
    # que los va a leer, y reordenarlos por nuestra cuenta rompería la
    # correspondencia con lo que tiene en pantalla.
    por_id = {p.id: p for p in productos}
    resultados: list[ResultadoCalculadora] = []
    for producto_id in solicitud.producto_ids:
        producto = por_id[producto_id]
        tasa = vigentes[producto_id]
        # El horizonte de un producto a la vista lo define el dominio, no el
        # router: se pasa por el modelo en vez de repetir la regla aquí.
        plazo = solicitud.plazo_dias or from_orm_producto(producto).plazo_efectivo_dias

        cascada = desglose_cascada(
            monto=solicitud.monto,
            tasa_nominal=tasa.tasa_nominal,
            instrumento=producto.instrumento,
            plazo_dias=plazo,
            inflacion_anual=inflacion,
            params=contexto.params_fiscales,
        )
        cobertura = resolver_cobertura(producto.institucion.tipo_seguro, contexto.valor_udi)

        resultados.append(
            ResultadoCalculadora(
                institucion=institucion_resumen(producto.institucion),
                producto_id=producto.id,
                producto=producto.nombre,
                cascada=CascadaSchema.model_validate(cascada, from_attributes=True),
                cobertura=cobertura_de(producto.institucion, contexto.valor_udi),
                monto_expuesto=cobertura.monto_expuesto(solicitud.monto),
                banderas=[bandera_desde_orm(b) for b in banderas.get(producto.institucion_id, [])],
                procedencia=procedencia(tasa),
            )
        )

    return RespuestaCalculadora(resultados=resultados, generado_en=datetime.now(UTC))


__all__ = ["router"]
