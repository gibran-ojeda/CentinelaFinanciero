"""Servicio del comparador: la vista principal del producto.

Traduce los filtros de §7 a una consulta y arma cada fila con las métricas de
`metrics/`. Los filtros que se pueden expresar en SQL se aplican en SQL; los
que dependen de un cálculo —`sin_banderas`, el orden por TEN o por GAT— se
resuelven después, porque no existen como columna.

Regla que atraviesa todo el módulo: **sólo se muestra lo publicable**. Un
producto sin tasa publicable no aparece en el comparador, aunque exista en el
catálogo: la vista principal no puede tener huecos.

Qué cuenta como publicable depende de `mostrar_tasas_sin_verificar`, que viaja
en el contexto de la petición. Apagada, publicable significa **tasa VIGENTE**.
Encendida —la política de transición del lanzamiento— entran también las tasas
en PENDIENTE_REVISION, pero cada fila lo declara en `procedencia.verificada`:
se amplía lo que se muestra, nunca lo que se afirma.
"""

from __future__ import annotations

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
    tramos_de,
    tramos_schema,
)
from api.services.tasas_vigentes import tasas_vigentes_por_producto
from domain.enums import CategoriaInstitucion, Liquidez, TipoProducto, TipoSeguro
from domain.orm import Bandera, Institucion, Producto, Tasa
from metrics.coverage import resolver_cobertura
from metrics.gat import Gat, resolver_gat
from metrics.ten import ten
from metrics.tramos import escalera_de, tasa_ponderada


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
    """Los filtros de §7.

    `seguros` y `categorias` son **conjuntos**: la UI los presenta como
    desplegables de selección múltiple, y "IPAB o PROSOFIPO" es una pregunta
    natural que un filtro de valor único no puede expresar. Conjunto vacío
    significa "todos", no "ninguno": es lo que hace que la ausencia del
    parámetro y el desplegable sin marcar den el mismo resultado.
    """

    plazo: str | None = None
    categorias: frozenset[CategoriaInstitucion] = frozenset()
    monto: Decimal | None = None
    seguros: frozenset[TipoSeguro] = frozenset()
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
    #: Solo cuando la petición trae monto: la nominal ponderada de la escalera
    #: a ese monto y su TEN. En productos planos coinciden con las titulares.
    tasa_efectiva: Decimal | None = None
    ten_efectiva: Decimal | None = None


def _aplicar_filtros_sql(
    consulta: Select[tuple[Producto]],
    f: FiltrosComparador,
) -> Select[tuple[Producto]]:
    """Los filtros que sí son columnas."""
    consulta = consulta.where(Producto.activo.is_(True), Institucion.activa.is_(True))

    # Invariante, no modo: una institución marcada como demostración jamás se
    # sirve. Desde la purga no existe ninguna, pero el predicado se queda para
    # que un respaldo restaurado o un seed viejo no la resuciten en público.
    consulta = consulta.where(Institucion.es_demostracion.is_(False))

    if f.plazo is not None:
        if f.plazo.upper() == PLAZO_VISTA:
            consulta = consulta.where(Producto.tipo == TipoProducto.VISTA)
        elif f.plazo == "365+":
            consulta = consulta.where(Producto.plazo_dias >= PLAZO_LARGO_DIAS)
        else:
            consulta = consulta.where(Producto.plazo_dias == int(f.plazo))

    if f.categorias:
        consulta = consulta.where(Institucion.categoria.in_(f.categorias))

    if f.monto is not None:
        # Excluye lo que el usuario no puede contratar con ese capital.
        consulta = consulta.where(Producto.monto_minimo <= f.monto)

    if f.liquidez is not None:
        consulta = consulta.where(Producto.liquidez == f.liquidez)

    if f.seguros:
        consulta = consulta.where(Institucion.tipo_seguro.in_(f.seguros))

    return consulta


def _clave_orden(candidato: _Candidato, orden: OrdenComparador) -> Decimal:
    # Con monto consultado mandan las efectivas: ordenar por la titular
    # pondría el «13% hasta $30 mil» de un escalonado por encima de un 9%
    # plano incluso para quien invierte $500,000. Sin monto, las titulares.
    match orden:
        case OrdenComparador.TASA_NOMINAL:
            return (
                candidato.tasa_efectiva
                if candidato.tasa_efectiva is not None
                else candidato.tasa.tasa_nominal
            )
        case OrdenComparador.TEN:
            return candidato.ten_efectiva if candidato.ten_efectiva is not None else candidato.ten
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

    vigentes = await tasas_vigentes_por_producto(
        session,
        [p.id for p in productos],
        incluir_pendientes=contexto.incluir_sin_verificar,
    )

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

        # Con monto en la petición, TODAS las filas llevan su efectiva — en
        # las planas coincide con la titular. La uniformidad simplifica el
        # orden y evita que el frontend mezcle columnas de dos semánticas.
        tasa_efectiva: Decimal | None = None
        ten_efectiva: Decimal | None = None
        if filtros.monto is not None:
            tasa_efectiva = tasa_ponderada(
                filtros.monto, escalera_de(tasa.tasa_nominal, tramos_de(tasa))
            )
            ten_efectiva = ten(tasa_efectiva, producto.instrumento, contexto.params_fiscales)

        candidatos.append(
            _Candidato(
                producto=producto,
                tasa=tasa,
                ten=ten(tasa.tasa_nominal, producto.instrumento, contexto.params_fiscales),
                gat=resolver_gat(
                    tasa.tasa_nominal,
                    contexto.inflacion_anual,
                    gat_publicada_nominal=tasa.gat_nominal,
                    gat_publicada_real=tasa.gat_real,
                ),
                cobertura_mxn=cobertura.limite_mxn,
                banderas=banderas_por_institucion.get(producto.institucion_id, []),
                tasa_efectiva=tasa_efectiva,
                ten_efectiva=ten_efectiva,
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
            escalonada=bool(c.tasa.tramos),
            tramos=tramos_schema(c.tasa),
            tasa_efectiva=c.tasa_efectiva,
            ten_efectiva=c.ten_efectiva,
        )
        for c in candidatos
    ]


__all__ = [
    "PLAZO_LARGO_DIAS",
    "PLAZO_VISTA",
    "FiltrosComparador",
    "OrdenComparador",
    "construir_comparador",
]
