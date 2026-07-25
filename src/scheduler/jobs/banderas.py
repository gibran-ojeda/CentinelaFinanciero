"""Job `banderas_recompute`: sincroniza la tabla `banderas` con los datos.

Recorre los indicadores más recientes de cada institución, ejecuta el motor de
`metrics.flags` y deja la tabla reflejando exactamente lo que las reglas dicen
hoy con los umbrales de hoy.

**Es idempotente y ese es el punto.** Dos corridas seguidas dejan el mismo
estado, así que puede ejecutarse tras cada ingesta, a diario, y a mano cuando
alguien cambie un umbral, sin acumular duplicados ni banderas fantasma.

Sincronizar significa tres cosas, no una:

1. Las banderas que las reglas emiten y no existían, se crean.
2. Las que existían y siguen emitiéndose, se dejan como están — conservando su
   `created_at`, que es desde cuándo la institución lleva marcada.
3. Las que existían y ya no se emiten, se **desactivan**, no se borran. La
   institución mejoró, y ese historial es parte de lo que el detalle muestra.

Doble gate como el resto de jobs: `SCHEDULER_BANDERAS_ENABLED` decide si se
registra, y `banderas_recompute_enabled` del ConfigStore lo apaga en caliente.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from statistics import median

from sqlalchemy import desc, select

from api.dependencies import umbrales_desde_config
from api.services import cache
from api.services.tasas_vigentes import tasas_vigentes_por_producto
from core.config_store import effective
from core.db import session_scope
from core.logging import get_logger
from domain.enums import TipoBandera
from domain.models import IndicadoresInstitucion, from_orm_indicadores
from domain.orm import Bandera, IndicadorFinanciero, Institucion, Producto
from metrics.flags import evaluar_banderas
from scheduler.bitacora import registrar_corrida

log = get_logger(__name__)

JOB_ID = "banderas_recompute"


@dataclass(frozen=True, slots=True)
class _Oferta:
    """La mejor tasa vigente de una institución, con lo que las reglas piden."""

    plazo_dias: int
    tasa_nominal: Decimal
    gat_nominal: Decimal | None


async def recomputar() -> dict[str, int]:
    """Sincroniza la tabla de banderas. Devuelve métricas de la corrida."""
    umbrales = umbrales_desde_config()
    metricas = {"instituciones": 0, "creadas": 0, "sin_cambios": 0, "desactivadas": 0}

    async with session_scope() as session:
        instituciones = (
            (await session.execute(select(Institucion).where(Institucion.activa.is_(True))))
            .scalars()
            .all()
        )

        # Mediana de tasa nominal por plazo: contexto de mercado que la
        # bandera compuesta de §5.2 necesita y que no depende de ninguna
        # institución en particular.
        #
        # La tasa se resuelve con el mismo servicio que usa el comparador y no
        # con un join directo contra `tasas`. `tasas` es append-only: unir
        # contra la tabla entera mete las observaciones históricas en la
        # mediana y hace que `mejor_oferta` elija el máximo de todos los
        # tiempos en vez del vigente.
        productos = (
            (
                await session.execute(
                    select(Producto).where(Producto.activo.is_(True)),
                )
            )
            .scalars()
            .all()
        )
        vigentes = await tasas_vigentes_por_producto(session, [p.id for p in productos])

        por_plazo: dict[int, list[Decimal]] = {}
        mejor_oferta: dict[int, _Oferta] = {}
        for producto in productos:
            tasa = vigentes.get(producto.id)
            if tasa is None:
                continue
            plazo = producto.plazo_dias or 0
            por_plazo.setdefault(plazo, []).append(tasa.tasa_nominal)
            mejor = mejor_oferta.get(producto.institucion_id)
            if mejor is None or tasa.tasa_nominal > mejor.tasa_nominal:
                mejor_oferta[producto.institucion_id] = _Oferta(
                    plazo_dias=plazo,
                    tasa_nominal=tasa.tasa_nominal,
                    gat_nominal=tasa.gat_nominal,
                )

        # `statistics.median`, no `sorted(...)[n // 2]`: con un número par de
        # productos el segundo devuelve el mayor de los dos centrales, que no
        # es la mediana. El comparador ya usaba la función correcta, así que
        # las dos mitades del sistema discrepaban sobre qué es "el mercado".
        medianas = {plazo: median(valores) for plazo, valores in por_plazo.items()}

        existentes: dict[int, list[Bandera]] = {}
        for fila_bandera in (
            (await session.execute(select(Bandera).where(Bandera.activa.is_(True))))
            .scalars()
            .all()
        ):
            existentes.setdefault(fila_bandera.institucion_id, []).append(fila_bandera)

        for institucion in instituciones:
            metricas["instituciones"] += 1

            fila = await session.scalar(
                select(IndicadorFinanciero)
                .where(IndicadorFinanciero.institucion_id == institucion.id)
                .order_by(desc(IndicadorFinanciero.periodo))
                .limit(1)
            )
            indicadores = (
                from_orm_indicadores(fila)
                if fila is not None
                # Sin datos de la CNBV sólo pueden emitirse las banderas que no
                # dependen de indicadores, como la de cobertura. Se arma un
                # objeto vacío con la fecha de hoy para que la bandera
                # estructural sí pueda salir.
                else IndicadoresInstitucion(
                    institucion_id=institucion.id, periodo=datetime.now(UTC).date()
                )
            )

            oferta = mejor_oferta.get(institucion.id)
            esperadas = evaluar_banderas(
                indicadores,
                umbrales,
                tipo_seguro=institucion.tipo_seguro,
                tasa_ofrecida=oferta.tasa_nominal if oferta else None,
                mediana_mercado=medianas.get(oferta.plazo_dias) if oferta else None,
                # Sin estos dos, `evaluar_gat_inconsistente` recibe None y
                # devuelve None siempre: la regla existía y estaba probada,
                # pero nada la alimentaba, así que la bandera 🟡 de GAT no
                # podía aparecer nunca en el producto.
                gat_publicada=oferta.gat_nominal if oferta else None,
                tasa_nominal=oferta.tasa_nominal if oferta else None,
            )

            actuales: dict[TipoBandera, Bandera] = {
                b.tipo: b for b in existentes.get(institucion.id, [])
            }
            deseadas: set[TipoBandera] = {b.tipo for b in esperadas}

            for bandera in esperadas:
                previa: Bandera | None = actuales.get(bandera.tipo)
                if previa is not None and previa.severidad == bandera.severidad:
                    # Se conserva `created_at`: es desde cuándo lleva marcada.
                    previa.motivo = bandera.motivo
                    previa.periodo_dato = bandera.periodo_dato
                    metricas["sin_cambios"] += 1
                    continue
                if previa is not None:
                    previa.activa = False
                    previa.resuelta_at = datetime.now(UTC)
                session.add(
                    Bandera(
                        institucion_id=bandera.institucion_id,
                        tipo=bandera.tipo,
                        severidad=bandera.severidad,
                        motivo=bandera.motivo,
                        periodo_dato=bandera.periodo_dato,
                        activa=True,
                    )
                )
                metricas["creadas"] += 1

            for tipo, anterior in actuales.items():
                if tipo not in deseadas:
                    # La institución mejoró: se desactiva, no se borra.
                    anterior.activa = False
                    anterior.resuelta_at = datetime.now(UTC)
                    metricas["desactivadas"] += 1

    return metricas


async def banderas_recompute() -> None:
    async with registrar_corrida(JOB_ID) as corrida:
        if not effective.banderas_recompute_enabled:
            corrida.omitir("banderas_recompute_enabled=false en ConfigStore")
            log.info("banderas_recompute_omitido")
            return

        metricas = await recomputar()
        corrida.metricas.update(metricas)

        if metricas["creadas"] or metricas["desactivadas"]:
            await cache.invalidar()

        log.info("banderas_recompute", **metricas)


__all__ = ["JOB_ID", "banderas_recompute", "recomputar"]
