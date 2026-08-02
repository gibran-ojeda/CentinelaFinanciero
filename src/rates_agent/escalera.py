"""Reconstrucción de escaleras por saldo desde extracciones del nivel 2.

El prompt del extractor pide «un tramo por monto es una entrada por tramo»:
Openbank llega como dos `TasaExtraida` del mismo `(tipo, plazo)` — 13% con
`monto_minimo` 0 y 6.3% con `monto_minimo` 30000. Este módulo convierte ese
grupo en UNA observación con su escalera completa, o lo declara
irreconstruible (⇒ hueco de catálogo) cuando los montos no alcanzan para
saber dónde corta cada tramo.

Vive en `rates_agent` y no en `metrics` porque su vocabulario es el del
agente (`TasaExtraida`); la validación matemática la delega en
`metrics.tramos.validar_escalera`, el constructor único de escaleras.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from metrics.tramos import Tramo, validar_escalera
from rates_agent.extractor import TasaExtraida

_CERO = Decimal("0")

#: Para quedarse con la peor del grupo: la escalera vale lo que su tramo
#: menos fiable.
_ORDEN_CONFIANZA: dict[str, int] = {"baja": 0, "media": 1, "alta": 2}


@dataclass(frozen=True, slots=True)
class EscaleraExtraida:
    """Una escalera completa reconstruida de N entradas del mismo producto."""

    tramos: tuple[Tramo, ...]
    cabeza: TasaExtraida
    """La entrada del tramo 1, con la confianza degradada a la peor del
    grupo: aporta la tasa titular, la GAT y el nombre del producto."""

    condiciones: str | None
    """Las condiciones del grupo: la común si todas dicen lo mismo, o la
    concatenación por tramo cuando difieren."""


def reconstruir_escalera(entradas: Sequence[TasaExtraida]) -> EscaleraExtraida | None:
    """N entradas del mismo `(tipo, plazo)` → una escalera, o `None` (⇒ hueco).

    Pisos: el `monto_minimo` de cada entrada (`None` → 0), ordenados. Techos:
    el piso de la siguiente; la última queda sin techo (∞). Es irreconstruible
    cuando hay menos de dos entradas, cuando dos comparten piso —no se sabe
    dónde corta el tramo— o cuando el piso mínimo no es 0: una escalera que no
    cubre desde el primer peso tiene un tramo base que la página no declaró, y
    la regla 1 del extractor prohíbe inventarlo.
    """
    if len(entradas) < 2:
        return None

    ordenadas = sorted(entradas, key=lambda e: e.monto_minimo or _CERO)
    pisos = [e.monto_minimo or _CERO for e in ordenadas]
    if len(set(pisos)) != len(pisos):
        return None
    if pisos[0] != _CERO:
        return None

    crudos = tuple(
        Tramo(
            desde=piso,
            hasta=pisos[indice + 1] if indice + 1 < len(pisos) else None,
            tasa_nominal=entrada.tasa_nominal,
        )
        for indice, (piso, entrada) in enumerate(zip(pisos, ordenadas, strict=True))
    )
    try:
        tramos = validar_escalera(crudos)
    except ValueError:
        return None
    if not tramos:
        # El validador normaliza a plana; con ≥2 pisos distintos no ocurre,
        # pero si ocurriera no habría escalera que persistir.
        return None

    peor = min((e.confianza for e in ordenadas), key=_ORDEN_CONFIANZA.__getitem__)
    cabeza = ordenadas[0].model_copy(update={"confianza": peor})
    return EscaleraExtraida(
        tramos=tramos,
        cabeza=cabeza,
        condiciones=_condiciones(ordenadas, tramos),
    )


def render_escalera(tramos: Sequence[Tramo]) -> str:
    """La escalera en una frase, para motivos de revisión y logs.

    `«$0–$30,000: 13.00% · $30,000 en adelante: 6.30%»` — es lo que ve quien
    aprueba en `cli revisiones list`, así que optimiza para leerse en una
    línea de terminal.
    """
    if not tramos:
        return "plana"
    return " · ".join(f"{_rango(t)}: {t.tasa_nominal}%" for t in tramos)


def _rango(tramo: Tramo) -> str:
    if tramo.hasta is None:
        return f"${tramo.desde:,.0f} en adelante"
    return f"${tramo.desde:,.0f}–${tramo.hasta:,.0f}"


def _condiciones(ordenadas: Sequence[TasaExtraida], tramos: Sequence[Tramo]) -> str | None:
    distintas = {e.condiciones for e in ordenadas if e.condiciones}
    if not distintas:
        return None
    if len(distintas) == 1:
        return next(iter(distintas))
    partes = [
        f"{_rango(tramo)}: {entrada.condiciones}"
        for tramo, entrada in zip(tramos, ordenadas, strict=True)
        if entrada.condiciones
    ]
    return " · ".join(partes)


__all__ = ["EscaleraExtraida", "reconstruir_escalera", "render_escalera"]
