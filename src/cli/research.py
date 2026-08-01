"""Reporte de calibración del nivel 3.

La fase 9 pide dos o tres semanas midiendo tasa de aprobación de la cola,
costo semanal y falsos positivos antes de dar por buena la búsqueda abierta —
con la barra en el 80 % de aprobación. Los datos ya se recogen solos:
`job_runs.metricas` guarda tokens, costo y hallazgos de cada corrida, y la
cola de revisión sabe qué se aprobó y qué se rechazó. Este módulo únicamente
los junta y los presenta; no hay nada que medir a mano.

El corte por fuente importa: `FETCH_DIRIGIDO` pasa por el mismo reviewer, así
que su tasa de aprobación es el término de comparación natural — si el nivel 3
aprueba mucho menos que el 2, lo que trae no vale lo que cuesta.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from core.config_store import effective
from core.db import session_scope
from domain.enums import EstadoJob, EstadoRevision, FuenteTasa
from domain.orm import JobRun, RevisionTasa, Tasa

JOB_RESEARCH = "tasas_research_abierta"

#: El techo de gasto es **compartido**: el extractor del fetch L2 y el
#: researcher pagan del mismo contador diario, así que el «máximo en un día»
#: suma las tres familias de corridas — medir solo el research subestimaría
#: qué tan cerca se anda del techo.
JOBS_CON_COSTO = (JOB_RESEARCH, "tasas_fetch_dirigido", "tasas_fetch_manual")

#: La barra de la fase 9: por debajo de esto, el nivel 3 trae más trabajo de
#: revisión del que resuelve.
BARRA_DE_APROBACION_PCT = 80


@dataclass(slots=True)
class SemanaResearch:
    """Los números de una semana ISO de corridas del researcher."""

    corridas: int = 0
    omitidas: int = 0
    investigadas: int = 0
    hallazgos: int = 0
    publicadas: int = 0
    en_revision: int = 0
    sin_datos: int = 0
    busquedas: int = 0
    tokens: int = 0
    costo_usd: float = 0.0
    huecos: int = 0
    degradadas: int = 0
    cortadas_por_presupuesto: int = 0


@dataclass(slots=True)
class TasaAprobacion:
    aprobadas: int = 0
    rechazadas: int = 0
    pendientes: int = 0

    @property
    def resueltas(self) -> int:
        return self.aprobadas + self.rechazadas

    @property
    def porcentaje(self) -> float | None:
        """Sobre resueltas, no sobre creadas: una pendiente aún no opina."""
        if not self.resueltas:
            return None
        return 100.0 * self.aprobadas / self.resueltas


@dataclass(slots=True)
class ReporteCalibracion:
    semanas: int
    por_semana: dict[str, SemanaResearch] = field(default_factory=dict)
    aprobacion: dict[FuenteTasa, TasaAprobacion] = field(default_factory=dict)
    gasto_max_dia_usd: float = 0.0
    dia_del_maximo: str | None = None
    techo_usd: float = 0.0

    def render(self) -> str:
        lineas: list[str] = []

        if not self.por_semana:
            lineas.append(f"  Sin corridas del researcher en las últimas {self.semanas} semanas.")
        else:
            cab = (
                f"  {'semana':<10} {'corridas':>8} {'invest.':>8} {'hallazg.':>8} "
                f"{'public.':>8} {'a revis.':>8} {'búsq.':>6} {'tokens':>9} {'costo USD':>10}"
            )
            lineas.append(cab)
            total = SemanaResearch()
            for etiqueta in sorted(self.por_semana):
                s = self.por_semana[etiqueta]
                lineas.append(
                    f"  {etiqueta:<10} {s.corridas:>8} {s.investigadas:>8} "
                    f"{s.hallazgos:>8} {s.publicadas:>8} {s.en_revision:>8} "
                    f"{s.busquedas:>6} {s.tokens:>9} {s.costo_usd:>10.6f}"
                )
                for campo in (
                    "corridas",
                    "omitidas",
                    "investigadas",
                    "hallazgos",
                    "publicadas",
                    "en_revision",
                    "sin_datos",
                    "busquedas",
                    "tokens",
                    "costo_usd",
                    "huecos",
                    "degradadas",
                    "cortadas_por_presupuesto",
                ):
                    setattr(total, campo, getattr(total, campo) + getattr(s, campo))
            lineas.append(
                f"  {'total':<10} {total.corridas:>8} {total.investigadas:>8} "
                f"{total.hallazgos:>8} {total.publicadas:>8} {total.en_revision:>8} "
                f"{total.busquedas:>6} {total.tokens:>9} {total.costo_usd:>10.6f}"
            )
            if total.huecos:
                lineas.append(
                    f"  huecos de catálogo reportados: {total.huecos} → "
                    f"python -m cli revisiones list"
                )
            if total.degradadas:
                lineas.append(
                    f"  ⚠ {total.degradadas} corridas degradadas (ningún motor respondió)"
                )
            if total.cortadas_por_presupuesto:
                lineas.append(
                    f"  ⚠ {total.cortadas_por_presupuesto} corridas cortadas por el techo"
                )

        if self.dia_del_maximo is not None:
            porcentaje = 100.0 * self.gasto_max_dia_usd / self.techo_usd if self.techo_usd else 0.0
            lineas.append(
                f"\n  gasto máximo en un día (L2+L3): ${self.gasto_max_dia_usd:.4f} de "
                f"${self.techo_usd:.2f} ({porcentaje:.1f} %) — {self.dia_del_maximo}"
            )

        lineas.append("\n  Cola de revisión creada en la ventana:")
        if not self.aprobacion:
            lineas.append("    (ninguna revisión nueva)")
        for fuente in (FuenteTasa.LLM_RESEARCH, FuenteTasa.FETCH_DIRIGIDO):
            stats = self.aprobacion.get(fuente)
            if stats is None:
                continue
            tasa = f"{stats.porcentaje:.0f} %" if stats.porcentaje is not None else "—"
            lineas.append(
                f"    {fuente.value:<15} aprobadas {stats.aprobadas} · "
                f"rechazadas {stats.rechazadas} → {tasa}  (pendientes {stats.pendientes})"
            )
        lineas.append(
            f"  La fase 9 pide ≥{BARRA_DE_APROBACION_PCT} % de aprobación para dar el "
            f"nivel 3 por calibrado."
        )
        return "\n".join(lineas)


async def reporte(semanas: int = 4) -> ReporteCalibracion:
    """Junta lo que las corridas y la cola ya registraron. Solo lee."""
    corte = datetime.now(UTC) - timedelta(weeks=semanas)
    salida = ReporteCalibracion(
        semanas=semanas, techo_usd=float(effective.llm_cost_daily_limit_usd)
    )

    async with session_scope() as session:
        corridas = (
            (
                await session.execute(
                    select(JobRun)
                    .where(JobRun.job_id.in_(JOBS_CON_COSTO), JobRun.inicio >= corte)
                    .order_by(JobRun.inicio)
                )
            )
            .scalars()
            .all()
        )
        filas_revision = (
            (
                await session.execute(
                    select(RevisionTasa.estado, Tasa.fuente)
                    .join(Tasa, Tasa.id == RevisionTasa.tasa_id)
                    .where(
                        RevisionTasa.created_at >= corte,
                        Tasa.fuente.in_((FuenteTasa.LLM_RESEARCH, FuenteTasa.FETCH_DIRIGIDO)),
                    )
                )
            )
            .tuples()
            .all()
        )

    gasto_por_dia: dict[str, float] = {}
    for corrida in corridas:
        # `metricas` es None cuando la corrida no anotó nada (bitácora).
        m = corrida.metricas or {}
        costo = float(m.get("costo_usd") or 0.0)
        if costo:
            dia = corrida.inicio.date().isoformat()
            gasto_por_dia[dia] = gasto_por_dia.get(dia, 0.0) + costo

        if corrida.job_id != JOB_RESEARCH:
            continue
        # Semana ISO en Python y no en SQL: los tests corren también sobre
        # SQLite y `date_trunc` es de Postgres.
        iso = corrida.inicio.date().isocalendar()
        semana = salida.por_semana.setdefault(f"{iso[0]}-W{iso[1]:02d}", SemanaResearch())
        semana.corridas += 1
        if corrida.estado is EstadoJob.OMITIDO:
            semana.omitidas += 1
        semana.investigadas += int(m.get("investigadas") or 0)
        semana.hallazgos += int(m.get("hallazgos") or 0)
        semana.publicadas += int(m.get("publicadas") or 0)
        semana.en_revision += int(m.get("en_revision") or 0)
        semana.sin_datos += int(m.get("sin_datos") or 0)
        semana.busquedas += int(m.get("busquedas") or 0)
        semana.tokens += int(m.get("tokens") or 0)
        semana.costo_usd += costo
        semana.huecos += len(m.get("huecos_catalogo") or [])
        # DEGRADADA vive en las métricas, no en el estado del JobRun.
        if m.get("estado") == "DEGRADADA" or m.get("degradada"):
            semana.degradadas += 1
        if m.get("presupuesto_agotado"):
            semana.cortadas_por_presupuesto += 1

    if gasto_por_dia:
        dia, gasto = max(gasto_por_dia.items(), key=lambda par: par[1])
        salida.gasto_max_dia_usd = gasto
        salida.dia_del_maximo = dia

    for estado, fuente in filas_revision:
        stats = salida.aprobacion.setdefault(fuente, TasaAprobacion())
        if estado is EstadoRevision.APROBADA:
            stats.aprobadas += 1
        elif estado is EstadoRevision.RECHAZADA:
            stats.rechazadas += 1
        else:
            stats.pendientes += 1

    return salida


__all__ = [
    "BARRA_DE_APROBACION_PCT",
    "JOBS_CON_COSTO",
    "ReporteCalibracion",
    "SemanaResearch",
    "TasaAprobacion",
    "reporte",
]
