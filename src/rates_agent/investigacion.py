"""La corrida del nivel 3: a quién investigar y qué hacer con lo que salga.

`researcher` sabe buscar una institución; esto sabe **cuáles** y qué pasa
después. La distinción importa porque a quién se investiga es la decisión que
mantiene el costo abajo: el nivel 3 cuesta varias llamadas al modelo por
institución, así que correrlo sobre el catálogo entero sería pagar por lo que el
nivel 2 ya resuelve más barato y mejor.

**Sólo se investigan las que lo necesitan**: sin fuente activa para el fetch
dirigido, o con la tasa vigente más vieja que el SLA de su fuente. Si el fetch
dirigido ya trajo la tasa de Klar, aquí no se busca a Klar. Y **una vez al día
por institución**: quien ya tiene lectura del researcher de hoy queda fuera de
la selección, que es lo que permite compartir la rejilla de 4 horas del fetch
sin multiplicar por seis el trabajo del modelo.

Lo que encuentre pasa por el **mismo `reviewer`** que el nivel 2, con
`fuente=LLM_RESEARCH`. La regla no cambia: la primera lectura de un producto
siempre la aprueba una persona, y ninguna decisión de publicar la toma un modelo.

Límite conocido, aceptado a propósito: el nivel 3 **no reconstruye escaleras
por saldo** (su esquema de hallazgos ni siquiera trae monto). Si un producto
escalonado aparece por aquí, cada hallazgo se procesa suelto: el primero se
escribe y el segundo choca con la idempotencia del día. El daño está acotado
porque esa lectura siempre es primera-lectura o Δ grande — nunca se publica
sola — y el remedio de fondo es llevar `rates_agent.escalera` también al
researcher cuando el nivel 2 haya rodado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.tasas_vigentes import tasas_vigentes_por_producto
from core.db import session_scope
from core.frescura import SLA_POR_FUENTE
from core.logging import get_logger
from domain.enums import EstadoTasa, FuenteTasa, TipoProducto
from domain.orm import FuenteTasas, Institucion, JobRun, Producto, Tasa
from llm.client import ClienteLLM
from llm.providers.base import ErrorPresupuestoAgotado, ErrorProveedor
from rates_agent.extractor import TasaExtraida
from rates_agent.researcher import Hallazgo, investigar
from rates_agent.reviewer import Decision, HuecoCatalogo, revisar
from rates_agent.search import SaludMotores, SearchExecutor

log = get_logger(__name__)

#: Corridas que escriben `instituciones` en sus métricas. Literal, y no
#: importado de `scheduler.jobs.research`, para no invertir la dependencia:
#: `rates_agent` es el motor y el scheduler lo llama, no al revés. Mismo patrón
#: que `cli/revisiones.py` y `cli/research.py`.
JOBS_DE_RESEARCH = ("tasas_research_abierta",)


@dataclass(slots=True)
class ReporteInvestigacion:
    """Qué pasó en la corrida de búsqueda abierta."""

    candidatas: int = 0
    investigadas: int = 0
    hallazgos: int = 0
    publicadas: int = 0
    en_revision: int = 0
    sin_cambio: int = 0
    sin_datos: int = 0
    descartados_por_url: int = 0
    busquedas: int = 0
    tokens: int = 0
    costo_usd: float = 0.0
    degradada: bool = False
    presupuesto_agotado: bool = False
    #: Mismo shape que los del nivel 2 (`HuecoCatalogo.como_dict()`): así
    #: `cli revisiones list` los agrega junto a los del fetch sin distinguir
    #: de qué nivel vinieron.
    huecos_catalogo: list[dict[str, Any]] = field(default_factory=list)
    #: A quién se **intentó** investigar, con o sin suerte. Es lo que lee el
    #: guard del día de la corrida siguiente: sin esta lista, una institución
    #: que terminó sin datos no dejaba rastro y volvía a la cola cada 4 horas.
    instituciones: list[str] = field(default_factory=list)
    errores: list[str] = field(default_factory=list)

    def como_metricas(self) -> dict[str, Any]:
        return {
            "candidatas": self.candidatas,
            "investigadas": self.investigadas,
            "instituciones": self.instituciones,
            "hallazgos": self.hallazgos,
            "publicadas": self.publicadas,
            "en_revision": self.en_revision,
            "sin_cambio": self.sin_cambio,
            "sin_datos": self.sin_datos,
            "descartados_por_url": self.descartados_por_url,
            "busquedas": self.busquedas,
            "tokens": self.tokens,
            "costo_usd": round(self.costo_usd, 6),
            "degradada": self.degradada,
            "presupuesto_agotado": self.presupuesto_agotado,
            "huecos_catalogo": self.huecos_catalogo,
            "errores": self.errores[:20],
        }

    def render(self) -> str:
        lineas = [
            f"  candidatas              {self.candidatas:>4}",
            f"  investigadas            {self.investigadas:>4}",
            f"  hallazgos               {self.hallazgos:>4}",
            f"    publicados            {self.publicadas:>4}",
            f"    a revisión            {self.en_revision:>4}",
            f"    sin cambio            {self.sin_cambio:>4}",
            f"  sin datos               {self.sin_datos:>4}",
            f"  búsquedas               {self.busquedas:>4}",
            f"  costo USD               {self.costo_usd:.6f}",
        ]
        if self.huecos_catalogo:
            lineas.append(f"  huecos de catálogo      {len(self.huecos_catalogo):>4}")
        if self.descartados_por_url:
            lineas.append(
                f"  ⚠ {self.descartados_por_url} hallazgos citaban una URL que "
                f"ninguna búsqueda devolvió"
            )
        if self.degradada:
            lineas.append("  ⚠ ningún motor de búsqueda respondió: corrida degradada")
        if self.presupuesto_agotado:
            lineas.append("  ⚠ el techo de gasto diario cortó la corrida")
        for error in self.errores[:10]:
            lineas.append(f"    - {error}")
        return "\n".join(lineas)


@dataclass(frozen=True, slots=True)
class Candidata:
    """Una institución que merece el nivel 3, y por qué."""

    id: int
    nombre: str
    categoria: str
    sitio: str | None
    motivo: str


async def correr(
    *,
    cliente: ClienteLLM | None = None,
    ejecutor: SearchExecutor | None = None,
    hoy: date | None = None,
    limite: int | None = None,
) -> ReporteInvestigacion:
    """Investiga las instituciones stale y encola lo que encuentre."""
    reporte = ReporteInvestigacion()
    propio = cliente is None
    cliente = cliente or ClienteLLM()
    hoy = hoy or datetime.now(UTC).date()

    try:
        candidatas = await _candidatas(hoy)
        reporte.candidatas = len(candidatas)
        # El circuito de los buscadores es de la corrida entera; las URLs
        # vistas, de cada institución. Antes viajaban juntas dentro del
        # ejecutor y por eso las quince empezaban de cero contra unos motores
        # que ya habían dicho 403 y 429.
        salud = SaludMotores()
        for candidata in candidatas[: limite or len(candidatas)]:
            # Un ejecutor por institución: una URL que salió buscando Klar no
            # autoriza un hallazgo de Stori.
            suyo = ejecutor or SearchExecutor(salud=salud)
            if suyo.sin_motores_sanos:
                # Sin un solo buscador en pie, las candidatas que quedan darían
                # cinco llamadas al modelo cada una para reformular consultas
                # que nadie va a atender. El 2026-08-02 fueron catorce.
                reporte.degradada = True
                log.warning(
                    "research_cortado_sin_buscadores",
                    motores=suyo.motores_en_circuito,
                    pendientes=len(candidatas) - reporte.investigadas,
                )
                break
            # Se anota **antes** de investigar. El guard del día se apoyaba en
            # filas `Tasa` escritas, así que sólo cubría el caso exitoso: las
            # catorce que fallaron volvían a ser candidatas en las cinco
            # corridas restantes del día, contra los mismos motores caídos.
            reporte.instituciones.append(candidata.nombre)
            try:
                await _investigar_una(reporte, cliente, suyo, candidata, hoy)
            except ErrorPresupuestoAgotado:
                reporte.presupuesto_agotado = True
                log.warning("research_cortado_por_presupuesto", institucion=candidata.nombre)
                break
            except ErrorProveedor as exc:
                reporte.errores.append(f"{candidata.nombre}: {exc}")
                log.warning("research_fallido", institucion=candidata.nombre, error=str(exc)[:200])
            reporte.investigadas += 1
    finally:
        if propio:
            await cliente.cerrar()

    log.info("research_corrida", **reporte.como_metricas())
    return reporte


async def _investigar_una(
    reporte: ReporteInvestigacion,
    cliente: ClienteLLM,
    ejecutor: SearchExecutor,
    candidata: Candidata,
    hoy: date,
) -> None:
    async with session_scope() as session:
        productos = await _productos_de(session, candidata.id)
        vigentes = await tasas_vigentes_por_producto(
            session, [p.id for p in productos], incluir_pendientes=True
        )
        contexto = _contexto(productos, vigentes)

    resultado = await investigar(
        cliente,
        institucion=candidata.nombre,
        categoria=candidata.categoria,
        sitio=candidata.sitio,
        productos=[_describir(p) for p in productos],
        contexto=contexto,
        ejecutor=ejecutor,
        hoy=hoy,
    )

    reporte.hallazgos += len(resultado.hallazgos)
    reporte.busquedas += resultado.busquedas
    reporte.tokens += resultado.tokens
    reporte.costo_usd += resultado.costo_usd
    reporte.descartados_por_url += len(resultado.descartados_por_url)
    if resultado.sin_datos:
        reporte.sin_datos += 1
    if resultado.busquedas and not resultado.urls_vistas:
        # Se buscó y ningún motor devolvió nada: la cadena entera cayó.
        reporte.degradada = True

    if not resultado.hallazgos:
        return

    async with session_scope() as session:
        productos = await _productos_de(session, candidata.id)
        por_clave = {(p.tipo.value, p.plazo_dias): p for p in productos}
        vigentes = await tasas_vigentes_por_producto(
            session, [p.id for p in productos], incluir_pendientes=True
        )
        for hallazgo in resultado.hallazgos:
            producto = por_clave.get((hallazgo.tipo.value, hallazgo.plazo_dias))
            if producto is None:
                # Igual que en el nivel 2: un plazo que el catálogo no conoce
                # es un hueco de catálogo, no una tasa que forzar al producto
                # más parecido — ni un error de texto libre, que era invisible
                # para `cli revisiones list`.
                reporte.huecos_catalogo.append(
                    HuecoCatalogo(
                        institucion=candidata.nombre,
                        producto=hallazgo.producto,
                        plazo_dias=hallazgo.plazo_dias,
                        tasa_nominal=hallazgo.tasa_nominal,
                        url=hallazgo.url,
                    ).como_dict()
                )
                log.info(
                    "hueco_catalogo_research",
                    institucion=candidata.nombre,
                    plazo=hallazgo.plazo_dias,
                )
                continue

            candidato = vigentes.get(producto.id)
            vigente = candidato if candidato and candidato.estado is EstadoTasa.VIGENTE else None
            decision = await revisar(
                session,
                _como_extraida(hallazgo),
                producto=producto,
                vigente=vigente,
                referencia=candidato if vigente is None else None,
                url=hallazgo.url,
                fuente=FuenteTasa.LLM_RESEARCH,
            )
            match decision.decision:
                case Decision.PUBLICADA:
                    reporte.publicadas += 1
                case Decision.EN_REVISION:
                    reporte.en_revision += 1
                case Decision.SIN_CAMBIO:
                    reporte.sin_cambio += 1


def _como_extraida(hallazgo: Hallazgo) -> TasaExtraida:
    """El hallazgo, en la forma que el reviewer ya sabe juzgar."""
    return TasaExtraida(
        producto=hallazgo.producto,
        tipo=hallazgo.tipo,
        plazo_dias=hallazgo.plazo_dias,
        tasa_nominal=hallazgo.tasa_nominal,
        gat_nominal=hallazgo.gat_nominal,
        gat_real=hallazgo.gat_real,
        condiciones=hallazgo.notas,
        confianza=hallazgo.confianza,
    )


async def _candidatas(hoy: date) -> list[Candidata]:
    """Instituciones que el nivel 2 no está cubriendo y nadie investigó hoy.

    Dos motivos para entrar, y ninguno es «por si acaso»:

    - **Sin fuente activa**: no hay URL curada, así que el fetch dirigido ni
      siquiera la intenta. Descubrirla es justo para lo que sirve el nivel 3.
    - **Stale**: su tasa vigente más reciente pasó del SLA de su fuente. O el
      fetch está fallando en silencio, o la institución movió su página.

    Y uno para salir: **ya se investigó hoy**. Los dos motivos de arriba son
    de estado puro, y el estado no cambia hasta que una persona aprueba la
    revisión — sin este corte, cada corrida del día repetiría la misma
    investigación sobre las mismas instituciones.
    """
    async with session_scope() as session:
        instituciones = (
            (
                await session.execute(
                    select(Institucion).where(
                        Institucion.activa.is_(True),
                        Institucion.es_demostracion.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )

        con_fuente = set(
            (
                await session.execute(
                    select(FuenteTasas.institucion_id).where(
                        FuenteTasas.activa.is_(True),
                        # Una portada de nivel 3 no cuenta como fuente del
                        # fetch dirigido: la institución cuyo único registro
                        # es esa portada es justo la candidata que el
                        # researcher existe para cubrir.
                        FuenteTasas.nivel <= 2,
                    )
                )
            )
            .scalars()
            .all()
        )

        ultimas = dict(
            (
                await session.execute(
                    select(Producto.institucion_id, func.max(Tasa.fecha_dato))
                    .join(Tasa, Tasa.producto_id == Producto.id)
                    .where(Tasa.estado == EstadoTasa.VIGENTE)
                    .group_by(Producto.institucion_id)
                )
            )
            .tuples()
            .all()
        )

        # A quién ya se investigó hoy. Es el guard de coste de la cadencia
        # corta: los motivos de arriba son de estado puro, y una institución
        # cuya lectura espera aprobación humana sigue «stale» en cada corrida
        # — sin esto, las seis del día la reinvestigarían entera. Y el ahorro
        # tiene que decidirse AQUÍ: la idempotencia del reviewer descarta la
        # escritura duplicada, pero sólo después de haber pagado el tool-loop.
        con_dato_hoy = set(
            (
                await session.execute(
                    select(Producto.institucion_id)
                    .join(Tasa, Tasa.producto_id == Producto.id)
                    .where(Tasa.fuente == FuenteTasa.LLM_RESEARCH, Tasa.fecha_dato == hoy)
                )
            )
            .scalars()
            .all()
        )
        intentadas_hoy = await _intentadas_hoy(session, hoy)

        candidatas: list[Candidata] = []
        for institucion in instituciones:
            if institucion.id in con_dato_hoy or institucion.nombre in intentadas_hoy:
                continue
            ultima = ultimas.get(institucion.id)
            sla = SLA_POR_FUENTE[FuenteTasa.FETCH_DIRIGIDO]
            if ultima is None:
                motivo = "sin ninguna tasa vigente"
            elif ultima < hoy - timedelta(days=sla):
                motivo = f"su última tasa es del {ultima} (SLA {sla} días)"
            elif institucion.id not in con_fuente:
                motivo = "sin fuente activa para el fetch dirigido"
            else:
                continue
            candidatas.append(
                Candidata(
                    id=institucion.id,
                    nombre=institucion.nombre,
                    categoria=institucion.categoria.value,
                    sitio=institucion.url_sitio,
                    motivo=motivo,
                )
            )

    for candidata in candidatas:
        log.info("research_candidata", institucion=candidata.nombre, motivo=candidata.motivo)
    return candidatas


async def _intentadas_hoy(session: Any, hoy: date) -> set[str]:
    """A quién se intentó investigar hoy, con o sin resultado.

    La otra mitad del guard, y la que faltaba. La de arriba mira filas `Tasa`
    escritas, así que sólo cubre el caso exitoso: el 2026-08-02 catorce de las
    quince instituciones terminaron sin datos —los buscadores estaban caídos—,
    ninguna escribió una fila, y las catorce seguían siendo candidatas en las
    cinco corridas restantes del día.

    Se lee de `job_runs.metricas` y no de una tabla nueva porque el precedente
    ya existe: `cli revisiones list` agrega así los huecos de catálogo de las
    corridas recientes.
    """
    filas = (
        (
            await session.execute(
                select(JobRun.metricas).where(
                    JobRun.job_id.in_(JOBS_DE_RESEARCH),
                    JobRun.inicio >= datetime.combine(hoy, time.min, tzinfo=UTC),
                )
            )
        )
        .scalars()
        .all()
    )
    intentadas: set[str] = set()
    for metricas in filas:
        if isinstance(metricas, dict):
            nombres = metricas.get("instituciones")
            if isinstance(nombres, list):
                intentadas.update(str(n) for n in nombres)
    return intentadas


async def _productos_de(session: AsyncSession, institucion_id: int) -> list[Producto]:
    return list(
        (
            await session.execute(
                select(Producto).where(
                    Producto.institucion_id == institucion_id, Producto.activo.is_(True)
                )
            )
        )
        .scalars()
        .all()
    )


def _describir(producto: Producto) -> str:
    if producto.tipo is TipoProducto.VISTA:
        return f"{producto.nombre} (a la vista)"
    return f"{producto.nombre} ({producto.plazo_dias} días)"


def _contexto(productos: list[Producto], vigentes: dict[int, Tasa]) -> str:
    """Lo último que se sabe, para que el modelo pueda contrastar."""
    lineas = []
    for producto in productos:
        tasa = vigentes.get(producto.id)
        if tasa is None:
            lineas.append(f"- {producto.nombre}: sin lectura previa")
        else:
            lineas.append(
                f"- {producto.nombre}: {tasa.tasa_nominal}% "
                f"(leída el {tasa.fecha_dato}, fuente {tasa.fuente.value})"
            )
    return "\n".join(lineas) or "sin lecturas previas"


__all__ = ["Candidata", "ReporteInvestigacion", "correr"]
