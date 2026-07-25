"""Motor de banderas de riesgo institucional (§5 del foundation).

Las banderas no califican el instrumento financiero sino **la salud de la
institución que lo respalda**. Una SOFIPO con IMOR alto puede ofrecer la mejor
tasa del mercado y ser mala idea justamente por eso.

Tres principios que el módulo respeta, y que son de producto antes que de
código:

1. **Señales, no dictámenes.** §10 y §19: son "señales orientativas basadas en
   datos públicos de la CNBV", no juicios de solvencia. Por eso cada bandera
   lleva un motivo legible y el periodo del dato que la originó — una bandera
   sin fecha, con el rezago de 1-3 meses que tiene la CNBV, sería engañosa.
2. **Sin dato no hay bandera.** Todos los indicadores son opcionales porque la
   CNBV no publica lo mismo para todas las figuras. Ante la ausencia se calla,
   nunca se supone lo peor.
3. **Umbrales inyectados.** El módulo recibe `UmbralesBanderas` y no importa
   ConfigStore, lo que lo mantiene puro y testeable. Quien llama arma el objeto
   desde `effective`.
"""

from __future__ import annotations

from decimal import Decimal

from domain.enums import NivelCapitalizacion, Severidad, TipoBandera, TipoSeguro
from domain.models import Bandera, IndicadoresInstitucion, UmbralesBanderas


def _bandera(
    indicadores: IndicadoresInstitucion,
    tipo: TipoBandera,
    severidad: Severidad,
    motivo: str,
    *,
    compuesta: bool = False,
) -> Bandera:
    return Bandera(
        institucion_id=indicadores.institucion_id,
        tipo=tipo,
        severidad=severidad,
        motivo=motivo,
        periodo_dato=indicadores.periodo,
        compuesta=compuesta,
    )


# ─── Banderas individuales (§5.1) ─────────────────────────────


def evaluar_imor(
    indicadores: IndicadoresInstitucion, umbrales: UmbralesBanderas
) -> Bandera | None:
    """Índice de morosidad: qué parte de la cartera está en mora."""
    imor = indicadores.imor
    if imor is None:
        return None

    if imor > umbrales.imor_roja:
        return _bandera(
            indicadores,
            TipoBandera.IMOR,
            Severidad.ROJA,
            f"Morosidad de {imor}%, por encima del {umbrales.imor_roja}% de alerta. "
            f"La institución tiene problemas para cobrar lo que prestó, y eso "
            f"presiona su liquidez y su capital.",
        )
    if imor >= umbrales.imor_amarilla:
        return _bandera(
            indicadores,
            TipoBandera.IMOR,
            Severidad.AMARILLA,
            f"Morosidad de {imor}%, en el rango de atención "
            f"({umbrales.imor_amarilla}%–{umbrales.imor_roja}%).",
        )
    return None


def evaluar_cobertura_cartera(
    indicadores: IndicadoresInstitucion, umbrales: UmbralesBanderas
) -> Bandera | None:
    """¿Tiene reservas suficientes para absorber su cartera vencida?"""
    icor = indicadores.icor
    if icor is None:
        return None

    if icor < umbrales.cobertura_roja:
        return _bandera(
            indicadores,
            TipoBandera.COBERTURA_CARTERA,
            Severidad.ROJA,
            f"Cobertura de cartera vencida del {icor}%, por debajo del "
            f"{umbrales.cobertura_roja}%. No tiene reservas para cubrir ni la mitad "
            f"larga de lo que ya está vencido.",
        )
    if icor < umbrales.cobertura_amarilla:
        return _bandera(
            indicadores,
            TipoBandera.COBERTURA_CARTERA,
            Severidad.AMARILLA,
            f"Cobertura de cartera vencida del {icor}%: sus reservas no alcanzan a "
            f"cubrir por completo la cartera en mora.",
        )
    return None


def evaluar_icap(
    indicadores: IndicadoresInstitucion, umbrales: UmbralesBanderas
) -> Bandera | None:
    """Índice de capitalización: capital propio frente a activos en riesgo."""
    icap = indicadores.icap
    if icap is None:
        return None

    if icap < umbrales.icap_roja:
        return _bandera(
            indicadores,
            TipoBandera.ICAP,
            Severidad.ROJA,
            f"Capitalización del {icap}%, por debajo del mínimo regulatorio de "
            f"{umbrales.icap_roja}%.",
        )
    if icap < umbrales.icap_amarilla:
        return _bandera(
            indicadores,
            TipoBandera.ICAP,
            Severidad.AMARILLA,
            f"Capitalización del {icap}%: cumple el mínimo regulatorio "
            f"({umbrales.icap_roja}%) pero con poca holgura.",
        )
    return None


def evaluar_nicap(indicadores: IndicadoresInstitucion) -> Bandera | None:
    """Categoría prudencial que la CNBV asigna a cada SOFIPO.

    No tiene umbrales configurables: los niveles los define la CNBV, no
    nosotros.
    """
    nivel = indicadores.nicap_nivel
    if nivel is None:
        return None

    if nivel in (NivelCapitalizacion.N3, NivelCapitalizacion.N4):
        return _bandera(
            indicadores,
            TipoBandera.NICAP,
            Severidad.ROJA,
            f"Nivel de capitalización {nivel.value} de la CNBV: por debajo del "
            f"requerimiento, sujeta a medidas correctivas.",
        )
    if nivel is NivelCapitalizacion.N2:
        return _bandera(
            indicadores,
            TipoBandera.NICAP,
            Severidad.AMARILLA,
            f"Nivel de capitalización {nivel.value} de la CNBV: cumple el "
            f"requerimiento pero sin holgura.",
        )
    return None


def evaluar_apalancamiento(
    indicadores: IndicadoresInstitucion, umbrales: UmbralesBanderas
) -> Bandera | None:
    """Pasivo frente a capital: cuánto debe por cada peso propio."""
    apalancamiento = indicadores.apalancamiento
    if apalancamiento is None:
        return None

    if apalancamiento > umbrales.apalancamiento_amarilla:
        return _bandera(
            indicadores,
            TipoBandera.APALANCAMIENTO,
            Severidad.AMARILLA,
            f"Apalancamiento de {apalancamiento.quantize(Decimal('0.01'))} veces su "
            f"capital, por encima de {umbrales.apalancamiento_amarilla}.",
        )
    return None


def evaluar_individuales(
    indicadores: IndicadoresInstitucion, umbrales: UmbralesBanderas
) -> list[Bandera]:
    """Las cinco reglas de §5.1, en orden de aparición en el documento."""
    candidatas = [
        evaluar_imor(indicadores, umbrales),
        evaluar_cobertura_cartera(indicadores, umbrales),
        evaluar_icap(indicadores, umbrales),
        evaluar_nicap(indicadores),
        evaluar_apalancamiento(indicadores, umbrales),
    ]
    return [b for b in candidatas if b is not None]


# ─── Bandera informativa de cobertura (§5.3) ──────────────────


def evaluar_cobertura_seguro(
    indicadores: IndicadoresInstitucion, tipo_seguro: TipoSeguro
) -> Bandera | None:
    """Ausencia de fondo de protección. Permanente e informativa (§5.3).

    No depende de ningún indicador financiero ni de ningún umbral: es una
    consecuencia de la figura regulatoria. Se emite siempre para IFPEs.
    """
    if tipo_seguro is not TipoSeguro.NINGUNO:
        return None
    return _bandera(
        indicadores,
        TipoBandera.SIN_COBERTURA,
        Severidad.AMARILLA,
        "Sin cobertura del IPAB ni de PROSOFIPO. Los fondos se mantienen en un "
        "fideicomiso segregado, que es una protección distinta y no equivalente "
        "a un seguro de depósitos.",
    )


__all__ = [
    "evaluar_apalancamiento",
    "evaluar_cobertura_cartera",
    "evaluar_cobertura_seguro",
    "evaluar_icap",
    "evaluar_imor",
    "evaluar_individuales",
    "evaluar_nicap",
]
