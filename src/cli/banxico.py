"""Comando `python -m cli banxico sync`.

Llama exactamente a lo mismo que el job diario. Un solo camino: si fueran dos,
el que se corre a mano acabaría comportándose distinto del que corre solo — es
la misma razón por la que `cli tasas fetch` reusa el pipeline del job semanal.

Lo que la CLI añade es `--desde`, para rellenar a mano un hueco de la serie sin
esperar a que la sincronización incremental lo alcance (nunca lo alcanzaría:
arranca desde lo último guardado, no desde el primer agujero).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from core.logging import get_logger
from ingest_banxico import materializer, sync
from ingest_banxico.client import ClienteSIE

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ReporteBanxico:
    """Las dos mitades de la corrida, para imprimirlas juntas."""

    series: sync.ReporteSync
    tasas: materializer.ReporteMaterializacion

    @property
    def hubo_errores(self) -> bool:
        return bool(self.series.errores)

    def render(self) -> str:
        return "\n".join(
            [
                "  series",
                self.series.render(),
                "  tasas de CETES",
                self.tasas.render(),
            ]
        )


async def correr_sync(*, desde: date | None = None) -> ReporteBanxico:
    """Sincroniza las series y publica lo que haya de CETES."""
    cliente = ClienteSIE()
    if not cliente.hay_token:
        await cliente.cerrar()
        raise RuntimeError(
            "BANXICO_TOKEN está vacío. Regístralo gratis en "
            "https://www.banxico.org.mx/SieAPIRest/service/v1/token y ponlo en el .env"
        )

    try:
        series = await sync.sincronizar(cliente=cliente, desde=desde)
    finally:
        await cliente.cerrar()

    return ReporteBanxico(series=series, tasas=await materializer.materializar())


__all__ = ["ReporteBanxico", "correr_sync"]
