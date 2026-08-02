"""Puente entre la base y el motor de portafolio.

`metrics.portfolio` no sabe de SQL: recibe `Candidato`s ya armados y devuelve
números. Este módulo hace la traducción en los dos sentidos y es el único sitio
donde se decide qué productos pueden entrar en una combinación.

La regla de publicabilidad es la misma del comparador y por el mismo motivo: si
una tasa no se muestra en la tabla, tampoco puede repartirse dinero sobre ella.
Que la calculadora aceptara lo que el comparador oculta sería una puerta
trasera al catálogo sin verificar.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.dependencies import ContextoMercado
from api.services.tasas_vigentes import tasas_vigentes_por_producto
from domain.enums import Severidad
from domain.orm import Bandera, Institucion, Producto, Tasa
from metrics.portfolio import Candidato


class Catalogo:
    """Los productos publicables, listos para el motor y para la respuesta."""

    def __init__(
        self,
        candidatos: list[Candidato],
        productos: dict[int, Producto],
        tasas: dict[int, Tasa],
        banderas: dict[int, list[Bandera]],
    ) -> None:
        self.candidatos = candidatos
        self.productos = productos
        self.tasas = tasas
        self.banderas = banderas
        self._por_id = {c.producto_id: c for c in candidatos}

    def seleccionar(self, producto_ids: Sequence[int]) -> list[Candidato]:
        """Los candidatos pedidos, en el orden en que se pidieron.

        Falla si alguno no es publicable en vez de omitirlo: una calculadora
        que devuelve menos instrumentos de los que recibió, en silencio, hace
        que el usuario compare un reparto distinto del que armó.
        """
        faltantes = [pid for pid in producto_ids if pid not in self._por_id]
        if faltantes:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"Sin tasa publicable para los productos {sorted(faltantes)}. "
                    f"Puede que no existan o que su tasa esté pendiente de verificación."
                ),
            )
        return [self._por_id[pid] for pid in producto_ids]


async def cargar_catalogo(session: AsyncSession, contexto: ContextoMercado) -> Catalogo:
    productos = (
        (
            await session.execute(
                select(Producto)
                .join(Institucion)
                .options(selectinload(Producto.institucion))
                .where(Producto.activo.is_(True), Institucion.activa.is_(True))
                # Invariante, no modo: una institución de demostración jamás
                # se sirve, exista o no la bandera de transición.
                .where(Institucion.es_demostracion.is_(False))
            )
        )
        .scalars()
        .all()
    )

    tasas = await tasas_vigentes_por_producto(
        session, [p.id for p in productos], incluir_pendientes=contexto.incluir_sin_verificar
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

    candidatos = [
        Candidato(
            producto_id=producto.id,
            institucion_id=producto.institucion_id,
            tipo_seguro=producto.institucion.tipo_seguro,
            instrumento=producto.instrumento,
            tasa_nominal=tasas[producto.id].tasa_nominal,
            plazo_dias=producto.plazo_dias,
            monto_minimo=producto.monto_minimo,
            tiene_bandera_roja=any(
                b.severidad is Severidad.ROJA for b in banderas.get(producto.institucion_id, [])
            ),
        )
        for producto in productos
        if producto.id in tasas
    ]

    return Catalogo(
        candidatos=candidatos,
        productos={p.id: p for p in productos},
        tasas=tasas,
        banderas=banderas,
    )


def narrativa(
    bruto: Decimal, isr: Decimal, inflacion: Decimal, real: Decimal, *, instrumentos: int
) -> str:
    """La frase de §6, con los números de este reparto.

    Es el entregable de la calculadora tanto como la cascada: el usuario tiene
    que poder repetirla en voz alta.
    """
    if instrumentos == 0:
        return "Agrega instrumentos o pulsa Optimizar para ver el desglose."

    def pesos(valor: Decimal) -> str:
        return f"${valor:,.2f}"

    if real < 0:
        return (
            f"De {pesos(bruto)} de ganancia bruta, {pesos(isr)} se van en impuestos y "
            f"{pesos(inflacion)} se los come la inflación: con estos supuestos pierdes "
            f"{pesos(abs(real))} de poder adquisitivo."
        )
    return (
        f"De {pesos(bruto)} de ganancia bruta, {pesos(isr)} se van en impuestos, "
        f"{pesos(inflacion)} se los come la inflación y {pesos(real)} son realmente tuyos."
    )


__all__ = ["Catalogo", "cargar_catalogo", "narrativa"]
