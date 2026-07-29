"""La cola de revisión, en un solo sitio.

La consultan y la resuelven dos caras: los endpoints admin y la CLI del
operador. Si cada una implementara «aprobar» por su cuenta acabarían con dos
ideas distintas de qué significa aprobar — y la que se usa a diario es la CLI,
así que la divergencia se descubriría tarde y desde el lado equivocado.

Vive en `api/services` porque ahí están los servicios de lectura que la CLI ya
importa (`tasas_vigentes`); el sentido de la dependencia no es nuevo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.logging import get_logger
from domain.enums import EstadoRevision, EstadoTasa
from domain.orm import Institucion, Producto, RevisionTasa, Tasa

log = get_logger(__name__)


class RevisionNoEncontrada(Exception):
    """No existe esa revisión, o la tasa a la que colgaba desapareció."""


class RevisionYaResuelta(Exception):
    """Resolver dos veces sobrescribiría quién decidió y cuándo."""


@dataclass(frozen=True, slots=True)
class RevisionConContexto:
    """Una fila de la cola con lo que hace falta para decidirla sin buscar nada."""

    id: int
    tasa_id: int
    institucion: str
    producto: str
    plazo_dias: int | None
    motivo: str
    valor_anterior: Decimal | None
    valor_nuevo: Decimal
    estado: EstadoRevision
    fuente_url: str | None
    creada_en: datetime

    @property
    def diferencia_pp(self) -> Decimal | None:
        if self.valor_anterior is None:
            return None
        return self.valor_nuevo - self.valor_anterior


async def listar(
    session: AsyncSession, *, estado: EstadoRevision = EstadoRevision.PENDIENTE
) -> list[RevisionConContexto]:
    """La cola, con institución, producto y URL en el mismo viaje.

    Quien revisa necesita saber de qué habla cada fila para poder decidir; una
    cola que sólo muestre ids obliga a buscarlos a mano y deja de resolverse en
    minutos.
    """
    filas = (
        (
            await session.execute(
                select(RevisionTasa, Tasa, Producto, Institucion.nombre)
                .join(Tasa, Tasa.id == RevisionTasa.tasa_id)
                .join(Producto, Producto.id == Tasa.producto_id)
                .join(Institucion, Institucion.id == Producto.institucion_id)
                .where(RevisionTasa.estado == estado)
                .order_by(Institucion.nombre, desc(RevisionTasa.created_at))
            )
        )
        .tuples()
        .all()
    )

    return [
        RevisionConContexto(
            id=revision.id,
            tasa_id=revision.tasa_id,
            institucion=institucion,
            producto=producto.nombre,
            plazo_dias=producto.plazo_dias,
            motivo=revision.motivo,
            valor_anterior=revision.valor_anterior,
            valor_nuevo=revision.valor_nuevo,
            estado=revision.estado,
            fuente_url=tasa.fuente_url,
            creada_en=revision.created_at,
        )
        for revision, tasa, producto, institucion in filas
    ]


async def resolver(
    session: AsyncSession,
    revision_id: int,
    *,
    aprobar: bool,
    revisor: str,
    comentario: str | None = None,
) -> RevisionTasa:
    """Aprueba (publica la tasa) o rechaza (la descarta). No borra nada.

    Devuelve la revisión ya resuelta. **No hace commit**: eso es del llamador,
    que también decide si invalida la cache.
    """
    revision = await session.get(RevisionTasa, revision_id)
    if revision is None:
        raise RevisionNoEncontrada(f"no existe la revisión {revision_id}")

    if revision.estado is not EstadoRevision.PENDIENTE:
        raise RevisionYaResuelta(f"la revisión {revision_id} ya está {revision.estado.value}")

    tasa = await session.get(Tasa, revision.tasa_id)
    if tasa is None:
        raise RevisionNoEncontrada(f"la tasa {revision.tasa_id} ya no existe")

    revision.estado = EstadoRevision.APROBADA if aprobar else EstadoRevision.RECHAZADA
    revision.revisor = revisor
    revision.resuelto_at = datetime.now(UTC)
    if comentario:
        revision.motivo = f"{revision.motivo} | {comentario}"

    tasa.estado = EstadoTasa.VIGENTE if aprobar else EstadoTasa.RECHAZADA

    log.info(
        "revision_resuelta",
        revision_id=revision_id,
        aprobada=aprobar,
        revisor=revisor,
        producto_id=tasa.producto_id,
    )
    return revision


__all__ = [
    "RevisionConContexto",
    "RevisionNoEncontrada",
    "RevisionYaResuelta",
    "listar",
    "resolver",
]
