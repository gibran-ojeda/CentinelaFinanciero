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


# ─── Banderas compuestas (§5.2) ───────────────────────────────


def evaluar_no_recomendable(
    indicadores: IndicadoresInstitucion, umbrales: UmbralesBanderas
) -> Bandera | None:
    """IMOR alto + ICAP bajo + captación creciendo agresivamente.

    El patrón que describe §5.2: la institución no cobra lo que prestó, tiene
    poco capital para absorberlo, y aun así capta cada vez más. Ninguno de los
    tres indicadores por separado significa esto; los tres juntos sí.
    """
    imor, icap = indicadores.imor, indicadores.icap
    crecimiento = indicadores.crecimiento_captacion_pct
    if imor is None or icap is None or crecimiento is None:
        return None

    if not (
        imor > umbrales.imor_roja
        and icap < umbrales.icap_amarilla
        and crecimiento > umbrales.crecimiento_captacion_pct
    ):
        return None

    return _bandera(
        indicadores,
        TipoBandera.NO_RECOMENDABLE,
        Severidad.ROJA,
        f"Combinación de riesgo: morosidad del {imor}%, capitalización del {icap}% y "
        f"crecimiento de captación del {crecimiento}%. Es el patrón de una "
        f"institución que capta para cubrir deudas previas.",
        compuesta=True,
    )


def evaluar_red_flag_tasa(
    indicadores: IndicadoresInstitucion,
    umbrales: UmbralesBanderas,
    *,
    tasa_ofrecida: Decimal | None,
    mediana_mercado: Decimal | None,
) -> Bandera | None:
    """Tasa muy por encima del mercado + morosidad en alerta.

    Una tasa alta no es sospechosa por sí sola: puede ser una institución
    eficiente compitiendo. Lo es cuando coincide con problemas de cobranza,
    porque entonces sugiere necesidad de liquidez y no eficiencia.
    """
    imor = indicadores.imor
    if imor is None or tasa_ofrecida is None or mediana_mercado is None:
        return None

    exceso = tasa_ofrecida - mediana_mercado
    if not (exceso > umbrales.tasa_sobre_mercado_pp and imor >= umbrales.imor_amarilla):
        return None

    return _bandera(
        indicadores,
        TipoBandera.RED_FLAG_TASA,
        Severidad.ROJA,
        f"Ofrece {tasa_ofrecida}% cuando la mediana del mercado a ese plazo es "
        f"{mediana_mercado}%, y su morosidad ({imor}%) está en alerta. Una tasa "
        f"muy por encima del mercado puede indicar necesidad de liquidez.",
        compuesta=True,
    )


def evaluar_gat_inconsistente(
    indicadores: IndicadoresInstitucion,
    umbrales: UmbralesBanderas,
    *,
    gat_publicada: Decimal | None,
    tasa_nominal: Decimal | None,
) -> Bandera | None:
    """GAT publicada que no cuadra con la tasa nominal. Amarilla, no roja."""
    from metrics.gat import gat_inconsistente

    if tasa_nominal is None:
        return None
    if not gat_inconsistente(gat_publicada, tasa_nominal, umbrales.gat_inconsistencia_pp):
        return None

    return _bandera(
        indicadores,
        TipoBandera.GAT_INCONSISTENTE,
        Severidad.AMARILLA,
        f"La GAT publicada ({gat_publicada}%) se aleja más de "
        f"{umbrales.gat_inconsistencia_pp} puntos de la tasa nominal ({tasa_nominal}%). "
        f"Conviene revisar comisiones o condiciones del producto.",
        compuesta=True,
    )


# ─── Resolución de prioridad ──────────────────────────────────

#: Orden de severidad para desempatar. Las compuestas rojas mandan.
_PESO_SEVERIDAD = {Severidad.ROJA: 2, Severidad.AMARILLA: 1}


def resolver_prioridad(banderas: list[Bandera]) -> list[Bandera]:
    """Aplica la nota de diseño de §5.2: se muestra sólo la más severa.

    Si hay una compuesta roja, se emite **sólo** esa: mostrarla junto a las
    individuales que la componen sería repetir el mismo hallazgo tres veces y
    dar la impresión de tres problemas donde hay uno.

    Excepción deliberada: `SIN_COBERTURA` sobrevive siempre. No es un hallazgo
    sobre la salud de la institución sino un hecho estructural sobre la figura
    regulatoria (§5.3), y el usuario necesita verlo aunque haya algo más grave.
    """
    if not banderas:
        return []

    permanentes = [b for b in banderas if b.tipo is TipoBandera.SIN_COBERTURA]
    evaluadas = [b for b in banderas if b.tipo is not TipoBandera.SIN_COBERTURA]

    compuestas_rojas = [b for b in evaluadas if b.compuesta and b.severidad is Severidad.ROJA]
    if compuestas_rojas:
        return [*compuestas_rojas, *permanentes]

    if not evaluadas:
        return permanentes

    maxima = max(_PESO_SEVERIDAD[b.severidad] for b in evaluadas)
    return [*[b for b in evaluadas if _PESO_SEVERIDAD[b.severidad] == maxima], *permanentes]


def evaluar_banderas(
    indicadores: IndicadoresInstitucion,
    umbrales: UmbralesBanderas,
    *,
    tipo_seguro: TipoSeguro | None = None,
    tasa_ofrecida: Decimal | None = None,
    mediana_mercado: Decimal | None = None,
    gat_publicada: Decimal | None = None,
    tasa_nominal: Decimal | None = None,
) -> list[Bandera]:
    """Punto de entrada del motor: evalúa todo y resuelve prioridad.

    `mediana_mercado` es el contexto de mercado que menciona §5.2 — lo calcula
    quien llama, porque depende del conjunto de productos comparables y no de
    esta institución.
    """
    candidatas = evaluar_individuales(indicadores, umbrales)

    compuestas = [
        evaluar_no_recomendable(indicadores, umbrales),
        evaluar_red_flag_tasa(
            indicadores,
            umbrales,
            tasa_ofrecida=tasa_ofrecida,
            mediana_mercado=mediana_mercado,
        ),
        evaluar_gat_inconsistente(
            indicadores,
            umbrales,
            gat_publicada=gat_publicada,
            tasa_nominal=tasa_nominal,
        ),
    ]
    candidatas.extend(b for b in compuestas if b is not None)

    if tipo_seguro is not None:
        sin_cobertura = evaluar_cobertura_seguro(indicadores, tipo_seguro)
        if sin_cobertura is not None:
            candidatas.append(sin_cobertura)

    return resolver_prioridad(candidatas)


__all__ = [
    "evaluar_apalancamiento",
    "evaluar_banderas",
    "evaluar_cobertura_cartera",
    "evaluar_cobertura_seguro",
    "evaluar_gat_inconsistente",
    "evaluar_icap",
    "evaluar_imor",
    "evaluar_individuales",
    "evaluar_nicap",
    "evaluar_no_recomendable",
    "evaluar_red_flag_tasa",
    "resolver_prioridad",
]
