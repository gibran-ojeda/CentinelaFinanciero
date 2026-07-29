"""Qué se publica y qué ve una persona. Determinista, sin LLM.

El modelo extrae; esto decide. La frontera es del foundation (§15) y no se
cruza: ninguna decisión sobre publicar un número la toma un modelo.

La regla que más pesa, y que no estaba en el plan original de la fase 9:
**la primera lectura de un producto siempre pasa por revisión.** Aunque
coincida al centavo con lo que decía el agregador. Coincidir con un dato sin
verificar no verifica nada — sólo dice que ambos vienen del mismo acierto o del
mismo error. Publicar automáticamente exige tener contra qué comparar, y eso
significa una `VIGENTE` previa que alguien ya aprobó.

Lo que sí es automático: la lectura número dos en adelante, cuando la
institución mueve su tasa dentro de la tolerancia. Ése es el caso frecuente —
las tasas se mueven décimas— y es donde el ahorro de trabajo está.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from core.config_store import effective
from core.logging import get_logger
from domain.enums import EstadoRevision, EstadoTasa, FuenteTasa
from domain.orm import Producto, RevisionTasa, Tasa
from rates_agent.extractor import TasaExtraida

log = get_logger(__name__)


class Decision(StrEnum):
    PUBLICADA = "PUBLICADA"
    """Dentro de tolerancia respecto a una vigente ya aprobada."""

    EN_REVISION = "EN_REVISION"
    """Queda pendiente y con fila en `revisiones_tasas`."""

    SIN_CAMBIO = "SIN_CAMBIO"
    """La institución publica lo mismo que ya está vigente. No se escribe nada."""


@dataclass(frozen=True, slots=True)
class Resultado:
    decision: Decision
    motivo: str
    tasa_id: int | None = None
    revision_id: int | None = None


@dataclass(slots=True)
class HuecoCatalogo:
    """Una tasa que no tiene dónde ir: la institución publica un plazo que el
    catálogo no conoce.

    No es una revisión pendiente — `revisiones_tasas` exige una `tasa_id`, y no
    hay tasa que crear sin producto. Es un hueco de catálogo, y el catálogo lo
    completa una persona en `seeds/productos.yaml`. Viaja en las métricas de la
    corrida para que `cli revisiones list` lo enseñe.
    """

    institucion: str
    producto: str
    plazo_dias: int | None
    tasa_nominal: Decimal
    url: str

    def como_dict(self) -> dict[str, str | int | None]:
        return {
            "institucion": self.institucion,
            "producto": self.producto,
            "plazo_dias": self.plazo_dias,
            "tasa_nominal": str(self.tasa_nominal),
            "url": self.url,
        }


@dataclass(slots=True)
class ReporteRevision:
    publicadas: int = 0
    en_revision: int = 0
    sin_cambio: int = 0
    huecos: list[HuecoCatalogo] = field(default_factory=list)

    def registrar(self, resultado: Resultado) -> None:
        match resultado.decision:
            case Decision.PUBLICADA:
                self.publicadas += 1
            case Decision.EN_REVISION:
                self.en_revision += 1
            case Decision.SIN_CAMBIO:
                self.sin_cambio += 1


def _diferencia(nueva: Decimal, anterior: Decimal) -> Decimal:
    return abs(nueva - anterior)


async def revisar(
    session: AsyncSession,
    extraida: TasaExtraida,
    *,
    producto: Producto,
    vigente: Tasa | None,
    referencia: Tasa | None,
    url: str,
    fecha_dato: date | None = None,
) -> Resultado:
    """Decide qué hacer con una tasa extraída y la escribe.

    Args:
        vigente: la `VIGENTE` del producto, si existe. Es lo único contra lo
            que se puede publicar automáticamente.
        referencia: contra qué comparar cuando no hay vigente — típicamente la
            fila `AGREGADOR`. **No autoriza a publicar**; sirve para que quien
            revise vea la diferencia enfrente.
    """
    fecha = fecha_dato or date.today()
    tolerancia = Decimal(str(effective.tolerancia_revision_pp))

    if vigente is not None and vigente.tasa_nominal == extraida.tasa_nominal:
        # Lo más común en un ciclo semanal: la institución no movió nada.
        # Escribir una observación idéntica cada lunes engordaría la tabla sin
        # añadir información.
        log.info("revision_sin_cambio", producto=producto.slug, tasa=str(extraida.tasa_nominal))
        return Resultado(
            Decision.SIN_CAMBIO, "la institución publica lo mismo que ya está vigente"
        )

    comparada = vigente or referencia
    diferencia = (
        _diferencia(extraida.tasa_nominal, comparada.tasa_nominal)
        if comparada is not None
        else None
    )

    motivo = _motivo(extraida, vigente, comparada, diferencia, tolerancia)

    tasa = Tasa(
        producto_id=producto.id,
        tasa_nominal=extraida.tasa_nominal,
        gat_nominal=extraida.gat_nominal,
        gat_real=extraida.gat_real,
        fecha_dato=fecha,
        fuente=FuenteTasa.FETCH_DIRIGIDO,
        fuente_url=url,
        estado=EstadoTasa.VIGENTE if motivo is None else EstadoTasa.PENDIENTE_REVISION,
        notas=extraida.condiciones,
    )
    session.add(tasa)
    await session.flush()

    # Se ramifica sobre `motivo is None` y no sobre una bandera aparte: el
    # motivo *es* la razón de no publicar, así que su ausencia y la publicación
    # son el mismo hecho — y así el tipo queda estrecho más abajo.
    if motivo is None:
        log.info(
            "revision_publicada",
            producto=producto.slug,
            tasa=str(extraida.tasa_nominal),
            anterior=str(comparada.tasa_nominal) if comparada else None,
        )
        return Resultado(Decision.PUBLICADA, "dentro de tolerancia", tasa_id=tasa.id)

    revision = RevisionTasa(
        tasa_id=tasa.id,
        motivo=motivo,
        valor_anterior=comparada.tasa_nominal if comparada else None,
        valor_nuevo=extraida.tasa_nominal,
        estado=EstadoRevision.PENDIENTE,
    )
    session.add(revision)
    await session.flush()

    log.info(
        "revision_encolada",
        producto=producto.slug,
        motivo=motivo,
        tasa=str(extraida.tasa_nominal),
    )
    return Resultado(Decision.EN_REVISION, motivo, tasa_id=tasa.id, revision_id=revision.id)


def _motivo(
    extraida: TasaExtraida,
    vigente: Tasa | None,
    comparada: Tasa | None,
    diferencia: Decimal | None,
    tolerancia: Decimal,
) -> str | None:
    """Por qué esta tasa necesita a una persona. `None` = se publica sola."""
    if vigente is None:
        procedencia = (
            f"; el dato de contraste ({comparada.fuente.value}) decía "
            f"{comparada.tasa_nominal}%"
            if comparada is not None
            else "; no hay ningún dato previo con el que contrastar"
        )
        return (
            f"Primera lectura oficial de este producto{procedencia}. "
            f"Coincidir con un dato sin verificar no lo verifica: la primera "
            f"publicación la aprueba una persona."
        )

    if extraida.confianza == "baja":
        return (
            f"El extractor declaró confianza baja sobre {extraida.tasa_nominal}% "
            f"(vigente {vigente.tasa_nominal}%)."
        )

    if diferencia is not None and diferencia > tolerancia:
        return (
            f"Cambio de {vigente.tasa_nominal}% a {extraida.tasa_nominal}% "
            f"({diferencia} pp), por encima de la tolerancia de {tolerancia} pp."
        )

    return None


__all__ = [
    "Decision",
    "HuecoCatalogo",
    "ReporteRevision",
    "Resultado",
    "revisar",
]
