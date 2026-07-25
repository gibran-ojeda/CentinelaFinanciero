"""Servicio del comparador: la vista principal del producto.

Traduce los filtros de §7 a una consulta y arma cada fila con las métricas de
`metrics/`. Los filtros que se pueden expresar en SQL se aplican en SQL; los
que dependen de un cálculo —`sin_banderas`, el orden por TEN o por GAT— se
resuelven después, porque no existen como columna.

Regla que atraviesa todo el módulo: **sólo se muestra lo publicable**. Un
producto sin tasa VIGENTE no aparece en el comparador, aunque exista en el
catálogo. La vista principal no puede tener huecos ni datos sin verificar.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.dependencies import ContextoMercado
from api.schemas import FilaComparador
from api.services.mappers import (
    bandera_desde_orm,
    cobertura_de,
    gat_schema,
    institucion_resumen,
    procedencia,
)
from api.services.tasas_vigentes import tasas_vigentes_por_producto
from domain.enums import CategoriaInstitucion, Liquidez, TipoProducto, TipoSeguro
from domain.orm import Bandera, Institucion, Producto, Tasa
from metrics.coverage import resolver_cobertura
from metrics.gat import Gat, resolver_gat
from metrics.ten import ten


class FiltroSeguro(StrEnum):
    """Filtro de cobertura de §7: "solo IPAB / solo Gobierno / todos"."""

    TODOS = "todos"
    SOLO_IPAB = "solo_ipab"
    SOLO_GOBIERNO = "solo_gobierno"
    CON_COBERTURA = "con_cobertura"
    """Cualquier fondo de protección. Excluye IFPEs."""


class OrdenComparador(StrEnum):
    TASA_NOMINAL = "tasa_nominal"
    TEN = "ten"
    GAT = "gat"
    COBERTURA = "cobertura"


#: Valor especial del filtro de plazo para productos a la vista.
PLAZO_VISTA = "VISTA"

#: A partir de aquí, "más de un año" (§7).
PLAZO_LARGO_DIAS = 365


@dataclass(frozen=True, slots=True)
class FiltrosComparador:
    plazo: str | None = None
    categoria: CategoriaInstitucion | None = None
    monto: Decimal | None = None
    seguro: FiltroSeguro = FiltroSeguro.TODOS
    liquidez: Liquidez | None = None
    sin_banderas: bool = False
    orden: OrdenComparador = OrdenComparador.TEN
    descendente: bool = True


@dataclass(slots=True)
class _Candidato:
    """Fila en construcción, con lo necesario para filtrar y ordenar."""

    producto: Producto
    tasa: Tasa
    ten: Decimal
    gat: Gat
    cobertura_mxn: Decimal | None
    banderas: list[Bandera] = field(default_factory=list)


def _aplicar_filtros_sql(consulta: Select[tuple[Producto]], f: FiltrosComparador) -> Select:
    """Los filtros que sí son columnas."""
    consulta = consulta.where(Producto.activo.is_(True), Institucion.activa.is_(True))

    if f.plazo is not None:
        if f.plazo.upper() == PLAZO_VISTA:
            consulta = consulta.where(Producto.tipo == TipoProducto.VISTA)
        elif f.plazo == "365+":
            consulta = consulta.where(Producto.plazo_dias >= PLAZO_LARGO_DIAS)
        else:
            consulta = consulta.where(Producto.plazo_dias == int(f.plazo))

    if f.categoria is not None:
        consulta = consulta.where(Institucion.categoria == f.categoria)

    if f.monto is not None:
        # Excluye lo que el usuario no puede contratar con ese capital.
        consulta = consulta.where(Producto.monto_minimo <= f.monto)

    if f.liquidez is not None:
        consulta = consulta.where(Producto.liquidez == f.liquidez)

    match f.seguro:
        case FiltroSeguro.SOLO_IPAB:
            consulta = consulta.where(Institucion.tipo_seguro == TipoSeguro.IPAB)
        case FiltroSeguro.SOLO_GOBIERNO:
            consulta = consulta.where(Institucion.tipo_seguro == TipoSeguro.SOBERANO)
        case FiltroSeguro.CON_COBERTURA:
            consulta = consulta.where(Institucion.tipo_seguro != TipoSeguro.NINGUNO)
        case _:
            pass

    return consulta


def _clave_orden(candidato: _Candidato, orden: OrdenComparador) -> Decimal:
    match orden:
        case OrdenComparador.TASA_NOMINAL:
            return candidato.tasa.tasa_nominal
        case OrdenComparador.TEN:
            return candidato.ten
        case OrdenComparador.GAT:
            # Usa la publicada y cae a la equivalente calculada. Ambas están ya
            # resueltas en `candidato.gat`, marcadas con su origen.
            return candidato.gat.nominal
        case OrdenComparador.COBERTURA:
            # Sin límite ordena por encima de cualquier importe: es más
            # cobertura, no menos, y tratarlo como cero lo hundiría al final.
            return (
                candidato.cobertura_mxn
                if candidato.cobertura_mxn is not None
                else Decimal("999999999999")
            )


def mediana_por_plazo(candidatos: Sequence[_Candidato]) -> dict[int, Decimal]:
    """Mediana de tasa nominal por plazo, para la bandera compuesta de §5.2.

    Es contexto de mercado y depende del conjunto comparado, no de la
    institución: por eso se calcula aquí y se inyecta en el motor de banderas.
    """
    por_plazo: dict[int, list[Decimal]] = {}
    for candidato in candidatos:
        plazo = candidato.producto.plazo_dias or 0
        por_plazo.setdefault(plazo, []).append(candidato.tasa.tasa_nominal)
    return {plazo: statistics.median(tasas) for plazo, tasas in por_plazo.items()}


async def construir_comparador(
    session: AsyncSession,
    contexto: ContextoMercado,
    filtros: FiltrosComparador,
) -> list[FilaComparador]:
    consulta = _aplicar_filtros_sql(
        select(Producto).join(Institucion).options(selectinload(Producto.institucion)),
        filtros,
    )
    productos = (await session.execute(consulta)).scalars().all()
    if not productos:
        return []

    vigentes = await tasas_vigentes_por_producto(session, [p.id for p in productos])

    banderas_por_institucion: dict[int, list[Bandera]] = {}
    filas_bandera = (
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
    )
    for bandera in filas_bandera:
        banderas_por_institucion.setdefault(bandera.institucion_id, []).append(bandera)

    candidatos: list[_Candidato] = []
    for producto in productos:
        tasa = vigentes.get(producto.id)
        if tasa is None:
            # Sin tasa publicable no hay fila: la vista principal no tiene
            # huecos ni muestra datos sin verificar.
            continue

        cobertura = resolver_cobertura(producto.institucion.tipo_seguro, contexto.valor_udi)
        candidatos.append(
            _Candidato(
                producto=producto,
                tasa=tasa,
                ten=ten(tasa.tasa_nominal, producto.instrumento, contexto.params_fiscales),
                gat=resolver_gat(
                    tasa.tasa_nominal,
                    producto.instrumento,
                    contexto.inflacion_anual,
                    contexto.params_fiscales,
                    gat_publicada_nominal=tasa.gat_nominal,
                    gat_publicada_real=tasa.gat_real,
                ),
                cobertura_mxn=cobertura.limite_mxn,
                banderas=banderas_por_institucion.get(producto.institucion_id, []),
            )
        )

    if filtros.sin_banderas:
        candidatos = [c for c in candidatos if not c.banderas]

    candidatos.sort(
        key=lambda c: (_clave_orden(c, filtros.orden), c.producto.slug),
        reverse=filtros.descendente,
    )

    return [
        FilaComparador(
            institucion=institucion_resumen(c.producto.institucion),
            producto_id=c.producto.id,
            producto=c.producto.nombre,
            producto_slug=c.producto.slug,
            tipo=c.producto.tipo,
            instrumento=c.producto.instrumento,
            plazo_dias=c.producto.plazo_dias,
            monto_minimo=c.producto.monto_minimo,
            liquidez=c.producto.liquidez,
            tasa_nominal=c.tasa.tasa_nominal,
            ten=c.ten,
            gat=gat_schema(c.gat),
            cobertura=cobertura_de(c.producto.institucion, contexto.valor_udi),
            banderas=[bandera_desde_orm(b) for b in c.banderas],
            procedencia=procedencia(c.tasa),
        )
        for c in candidatos
    ]


__all__ = [
    "PLAZO_LARGO_DIAS",
    "PLAZO_VISTA",
    "FiltroSeguro",
    "FiltrosComparador",
    "OrdenComparador",
    "construir_comparador",
    "mediana_por_plazo",
]
