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
from api.schemas import AlternativaSchema
from api.services.mappers import tramos_de
from api.services.tasas_vigentes import tasas_vigentes_por_producto
from domain.enums import Severidad
from domain.models import ParametrosFiscales
from domain.orm import Bandera, Institucion, Producto, Tasa
from metrics.portfolio import Candidato, evaluar_reparto, mejor_unico, referencia_cetes


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
            tramos=tramos_de(tasas[producto.id]),
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


def alternativas_de(
    catalogo: Catalogo,
    *,
    monto_total: Decimal,
    horizonte_dias: int,
    inflacion_anual: Decimal,
    params: ParametrosFiscales,
    valor_udi: Decimal,
    excluir_rojas: bool,
) -> list[AlternativaSchema]:
    """Las referencias contra las que se compara un reparto.

    Se calculan sobre el catálogo ya cargado: cero consultas nuevas por
    request. La etiqueta es descriptiva — el adjetivo «mejor» no viaja en
    ella; el criterio de selección lo documenta el schema (criterio 4 de los
    criterios de redacción: el número con su criterio a la vista).
    """
    alternativas: list[AlternativaSchema] = []

    cetes = referencia_cetes(
        catalogo.candidatos,
        monto_total=monto_total,
        horizonte_dias=horizonte_dias,
        excluir_rojas=excluir_rojas,
    )
    if cetes is not None:
        evaluado = evaluar_reparto(
            [cetes],
            [monto_total],
            horizonte_dias=horizonte_dias,
            inflacion_anual=inflacion_anual,
            params=params,
            valor_udi=valor_udi,
        )
        etiqueta = (
            f"Todo en CETES a {cetes.plazo_dias} días"
            if cetes.plazo_dias is not None
            else "Todo en CETES"
        )
        alternativas.append(
            AlternativaSchema(
                clave="todo_cetes",
                etiqueta=etiqueta,
                ten_ponderada=evaluado.ten_ponderada,
                ganancia_real=evaluado.ganancia_real,
                porcentaje_protegido=evaluado.porcentaje_protegido,
            )
        )

    unico = mejor_unico(
        catalogo.candidatos,
        monto_total=monto_total,
        horizonte_dias=horizonte_dias,
        inflacion_anual=inflacion_anual,
        params=params,
        valor_udi=valor_udi,
        excluir_rojas=excluir_rojas,
    )
    if unico is not None:
        candidato, evaluado = unico
        producto = catalogo.productos[candidato.producto_id]
        alternativas.append(
            AlternativaSchema(
                clave="mejor_unico",
                etiqueta=f"Todo en {producto.institucion.nombre} — {producto.nombre}",
                ten_ponderada=evaluado.ten_ponderada,
                ganancia_real=evaluado.ganancia_real,
                porcentaje_protegido=evaluado.porcentaje_protegido,
            )
        )

    return alternativas


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


__all__ = ["Catalogo", "alternativas_de", "cargar_catalogo", "narrativa"]
