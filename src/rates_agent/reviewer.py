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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config_store import effective
from core.logging import get_logger
from domain.enums import EstadoRevision, EstadoTasa, FuenteTasa
from domain.orm import Producto, RevisionTasa, Tasa, TramoTasa
from metrics.tramos import Tramo
from rates_agent.escalera import EscaleraExtraida, render_escalera
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


def _tramos_de(tasa: Tasa) -> tuple[Tramo, ...]:
    return tuple(
        Tramo(desde=t.desde, hasta=t.hasta, tasa_nominal=t.tasa_nominal) for t in tasa.tramos
    )


def _misma_estructura(previos: tuple[Tramo, ...], nuevos: tuple[Tramo, ...]) -> bool:
    """Los mismos cortes: igual número de tramos con iguales pisos y techos."""
    return len(previos) == len(nuevos) and all(
        p.desde == n.desde and p.hasta == n.hasta for p, n in zip(previos, nuevos, strict=True)
    )


def _delta(
    extraida: TasaExtraida,
    vigente: Tasa | None,
    comparada: Tasa | None,
    escalera: EscaleraExtraida | None,
) -> Decimal | None:
    """Cuánto se movió la lectura respecto a lo comparado.

    Con escaleras de la misma estructura, el delta es el **máximo por tramo**:
    el criterio más conservador que sigue explicándose en una frase. Si la
    estructura cambió, `_motivo` manda a revisión antes de mirar este número,
    así que el delta de titulares basta como respaldo.
    """
    if comparada is None:
        return None
    if vigente is not None and escalera is not None:
        previos = _tramos_de(vigente)
        if _misma_estructura(previos, escalera.tramos):
            return max(
                _diferencia(n.tasa_nominal, p.tasa_nominal)
                for p, n in zip(previos, escalera.tramos, strict=True)
            )
    return _diferencia(extraida.tasa_nominal, comparada.tasa_nominal)


async def revisar(
    session: AsyncSession,
    extraida: TasaExtraida,
    *,
    producto: Producto,
    vigente: Tasa | None,
    referencia: Tasa | None,
    url: str,
    fecha_dato: date | None = None,
    fuente: FuenteTasa = FuenteTasa.FETCH_DIRIGIDO,
    escalera: EscaleraExtraida | None = None,
) -> Resultado:
    """Decide qué hacer con una tasa extraída y la escribe.

    Args:
        vigente: la `VIGENTE` del producto, si existe. Es lo único contra lo
            que se puede publicar automáticamente.
        referencia: contra qué comparar cuando no hay vigente — típicamente la
            fila `AGREGADOR`. **No autoriza a publicar**; sirve para que quien
            revise vea la diferencia enfrente.
        fuente: `FETCH_DIRIGIDO` para el nivel 2 y `LLM_RESEARCH` para el 3.
            Cambia de dónde vino el dato, **no las reglas**: las dos pasan por
            aquí y las dos exigen que la primera lectura la apruebe una persona.
        escalera: los tramos por saldo reconstruidos, cuando la institución
            publica varios. `extraida` es entonces la cabeza (el tramo 1) y la
            escalera entera es UNA observación: se persiste como hijos de la
            misma `Tasa` y cuenta una sola vez bajo `uq_tasa_observacion`.
    """
    fecha = fecha_dato or date.today()
    tolerancia = Decimal(str(effective.tolerancia_revision_pp))

    # `tasas` tiene clave única `(producto, fecha, fuente)`, así que una
    # segunda corrida el mismo día chocaría contra la observación de la
    # primera. Pasa todos los días: el job corre cada 4 horas, y también en
    # el reintento de una corrida que falló a la mitad. Se comprueba antes de
    # escribir para que reintentar sea barato en vez de imposible.
    ya_registrada = await session.scalar(
        select(Tasa).where(
            Tasa.producto_id == producto.id,
            Tasa.fecha_dato == fecha,
            Tasa.fuente == fuente,
        )
    )
    if ya_registrada is not None:
        log.info(
            "revision_ya_registrada_hoy",
            producto=producto.slug,
            tasa=str(ya_registrada.tasa_nominal),
        )
        return Resultado(
            Decision.SIN_CAMBIO,
            "ya hay una lectura de esta fuente para hoy",
            tasa_id=ya_registrada.id,
        )

    if (
        vigente is not None
        and vigente.tasa_nominal == extraida.tasa_nominal
        and _tramos_de(vigente) == (escalera.tramos if escalera is not None else ())
    ):
        # Lo más común: la institución no movió nada. Escribir una observación
        # idéntica en cada corrida engordaría la tabla sin añadir información.
        # La comparación incluye la escalera completa: una vigente plana
        # contra una lectura escalonada del mismo titular NO es «sin cambio» —
        # es justo el dato nuevo que hay que revisar.
        log.info("revision_sin_cambio", producto=producto.slug, tasa=str(extraida.tasa_nominal))
        return Resultado(
            Decision.SIN_CAMBIO, "la institución publica lo mismo que ya está vigente"
        )

    comparada = vigente or referencia
    diferencia = _delta(extraida, vigente, comparada, escalera)

    motivo = _motivo(extraida, vigente, comparada, diferencia, tolerancia, escalera)

    tasa = Tasa(
        producto_id=producto.id,
        tasa_nominal=extraida.tasa_nominal,
        gat_nominal=extraida.gat_nominal,
        gat_real=extraida.gat_real,
        fecha_dato=fecha,
        fuente=fuente,
        fuente_url=url,
        estado=EstadoTasa.VIGENTE if motivo is None else EstadoTasa.PENDIENTE_REVISION,
        notas=escalera.condiciones if escalera is not None else extraida.condiciones,
    )
    session.add(tasa)
    await session.flush()

    if escalera is not None:
        session.add_all(
            TramoTasa(tasa_id=tasa.id, desde=t.desde, hasta=t.hasta, tasa_nominal=t.tasa_nominal)
            for t in escalera.tramos
        )
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
    escalera: EscaleraExtraida | None,
) -> str | None:
    """Por qué esta tasa necesita a una persona. `None` = se publica sola."""
    leida = (
        f"la escalera {render_escalera(escalera.tramos)}"
        if escalera is not None
        else f"{extraida.tasa_nominal}%"
    )

    if vigente is None:
        procedencia = (
            f"; el dato de contraste ({comparada.fuente.value}) decía "
            f"{comparada.tasa_nominal}%"
            if comparada is not None
            else "; no hay ningún dato previo con el que contrastar"
        )
        return (
            f"Primera lectura oficial de este producto: {leida}{procedencia}. "
            f"Coincidir con un dato sin verificar no lo verifica: la primera "
            f"publicación la aprueba una persona."
        )

    if extraida.confianza == "baja":
        return (
            f"El extractor declaró confianza baja sobre {leida} "
            f"(vigente {vigente.tasa_nominal}%)."
        )

    previos = _tramos_de(vigente)
    nuevos = escalera.tramos if escalera is not None else ()
    if not _misma_estructura(previos, nuevos):
        # Cambió QUÉ es el producto —de plano a escalonado, o los cortes— y
        # eso no es un movimiento de décimas que la tolerancia sepa juzgar:
        # siempre lo mira una persona, con ambas escaleras enfrente.
        antes = render_escalera(previos) if previos else f"plana ({vigente.tasa_nominal}%)"
        ahora = render_escalera(nuevos) if nuevos else f"plana ({extraida.tasa_nominal}%)"
        return f"La escalera cambió de estructura: antes {antes}; ahora {ahora}."

    if diferencia is not None and diferencia > tolerancia:
        if escalera is not None:
            return (
                f"Algún tramo se movió {diferencia} pp, por encima de la "
                f"tolerancia de {tolerancia} pp: antes {render_escalera(previos)}; "
                f"ahora {render_escalera(nuevos)}."
            )
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
