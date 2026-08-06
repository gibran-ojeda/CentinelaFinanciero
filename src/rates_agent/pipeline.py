"""La corrida completa: leer, extraer y decidir, fuente por fuente.

Una sola función para el job programado y para el comando de la CLI: dos
caminos acabarían divergiendo, y la corrida a mano dejaría de reproducir a la
programada justo cuando se usa para depurarla.

El orden importa y ahorra dinero:

1. `Fetcher` trae la página. Si ninguna capa lo consigue, esa fuente se salta y
   la corrida sigue: un sitio caído no puede costar las otras diecisiete.
2. **Si el hash del contenido es el de la corrida anterior, no se llama al
   LLM.** Las tasas se mueven poco; en la mayoría de las corridas la mayoría
   de las páginas son idénticas, y pagar por releerlas sería pagar por nada —
   es lo que hace viable correr esto cada 4 horas.
3. El extractor convierte el texto en tasas. El reviewer decide.

Un plazo que el catálogo no conoce no se fuerza contra el producto más
parecido: se reporta como hueco. Encajar 360 días en el producto de 364 porque
«es casi lo mismo» es exactamente el error que trajo el dato del agregador.

Dos techos cortan la corrida en seco y dejan lo que falte para la siguiente:
el de gasto (`llm_cost_daily_limit_usd`) y el de duración
(`tasas_fetch_minutos_max`). Ninguno es un fallo — con una corrida cada cuatro
horas, media lectura hoy vale más que una corrida que no termina.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from sqlalchemy import select

from api.services.tasas_vigentes import tasas_vigentes_por_producto
from core.config_store import effective
from core.db import session_scope
from core.logging import get_logger
from domain.enums import EstadoTasa, TipoProducto
from domain.orm import FuenteTasas, Institucion, Producto
from llm.client import ClienteLLM
from llm.providers.base import ErrorPresupuestoAgotado, ErrorProveedor
from rates_agent.escalera import reconstruir_escalera
from rates_agent.extractor import TasaExtraida, extraer
from rates_agent.fetcher import CadenaAgotada, Fetcher, una_linea
from rates_agent.reviewer import Decision, HuecoCatalogo, revisar

log = get_logger(__name__)


@dataclass(slots=True)
class ReporteCorrida:
    """Qué pasó, en números. Va a `job_runs.metricas`."""

    fuentes: int = 0
    leidas: int = 0
    sin_cambios_en_la_pagina: int = 0
    vacias: int = 0
    fallidas: int = 0
    publicadas: int = 0
    en_revision: int = 0
    sin_cambio: int = 0
    tasas_extraidas: int = 0
    hosts_en_circuito: list[str] = field(default_factory=list)
    huecos_catalogo: list[dict[str, Any]] = field(default_factory=list)
    #: Se leyó la página y el extractor no encontró ni una tasa. No es un
    #: error: es una URL que apunta a donde no hay nada que leer, y sin
    #: nombrarla se confunde con una lectura buena que no se movió.
    sin_tasas: list[str] = field(default_factory=list)
    #: Fuentes que esta corrida apagó por acumular fallos.
    fuentes_pausadas: list[str] = field(default_factory=list)
    presupuesto_agotado: bool = False
    cortada_por_tiempo: bool = False
    errores: list[str] = field(default_factory=list)

    @property
    def fracaso_total(self) -> bool:
        """Todas las fuentes fallaron: eso no es una corrida, es un fallo con bucle.

        Las vacías y las «sin cambios» cuentan como éxito — leer una página
        que no trae tasas es un resultado. Y los dos cortes —presupuesto y
        tiempo— dejan fuentes sin intentar, no fallidas, así que nunca
        disparan esto.
        """
        return self.fuentes > 0 and self.fallidas >= self.fuentes

    def como_metricas(self) -> dict[str, Any]:
        return {
            "fuentes": self.fuentes,
            "leidas": self.leidas,
            "sin_cambios_en_la_pagina": self.sin_cambios_en_la_pagina,
            "vacias": self.vacias,
            "fallidas": self.fallidas,
            "publicadas": self.publicadas,
            "en_revision": self.en_revision,
            "sin_cambio": self.sin_cambio,
            "tasas_extraidas": self.tasas_extraidas,
            "hosts_en_circuito": self.hosts_en_circuito,
            "huecos_catalogo": self.huecos_catalogo,
            "sin_tasas": self.sin_tasas,
            "fuentes_pausadas": self.fuentes_pausadas,
            "presupuesto_agotado": self.presupuesto_agotado,
            "cortada_por_tiempo": self.cortada_por_tiempo,
            "errores": self.errores[:20],
        }

    def render(self) -> str:
        lineas = [
            f"  fuentes                 {self.fuentes:>4}",
            f"  leídas                  {self.leidas:>4}",
            f"  sin cambios en la web   {self.sin_cambios_en_la_pagina:>4}  (sin costo LLM)",
            f"  sin tasas visibles      {self.vacias:>4}",
            f"  fallidas                {self.fallidas:>4}",
            f"  tasas extraídas         {self.tasas_extraidas:>4}",
            f"    publicadas            {self.publicadas:>4}",
            f"    a revisión            {self.en_revision:>4}",
            f"    sin cambio            {self.sin_cambio:>4}",
        ]
        if self.hosts_en_circuito:
            lineas.append(f"  hosts en circuito       {', '.join(self.hosts_en_circuito)}")
        if self.huecos_catalogo:
            lineas.append(f"  huecos de catálogo      {len(self.huecos_catalogo):>4}")
        if self.sin_tasas:
            lineas.append(f"  se leyeron sin tasas    {', '.join(self.sin_tasas)}")
        for pausada in self.fuentes_pausadas:
            lineas.append(f"  ⚠ fuente pausada: {pausada}")
        if self.presupuesto_agotado:
            lineas.append("  ⚠ el techo de gasto diario cortó la corrida")
        if self.cortada_por_tiempo:
            lineas.append("  ⚠ el techo de duración cortó la corrida")
        for error in self.errores[:10]:
            lineas.append(f"    - {error}")
        return "\n".join(lineas)


async def correr(
    *,
    fetcher: Fetcher | None = None,
    cliente: ClienteLLM | None = None,
    solo_requieren_js: bool | None = None,
) -> ReporteCorrida:
    """Recorre las fuentes activas y deja el resultado en la base.

    Args:
        solo_requieren_js: `True` corre sólo las páginas que necesitan
            navegador; `False`, sólo las que no. `None` (por defecto), todas.
            Es el filtro de depuración de la CLI — repetir una mitad de la
            corrida sin pagar la otra — y el que usa el repliegue
            `tasas_fetch_solo_sin_js`.
    """
    reporte = ReporteCorrida()
    propios = fetcher is None or cliente is None
    fetcher = fetcher or Fetcher()
    cliente = cliente or ClienteLLM()

    try:
        fuentes = await _fuentes(solo_requieren_js)
        reporte.fuentes = len(fuentes)
        # `monotonic` y no `now()`: un ajuste de reloj a mitad de corrida no
        # puede cortarla ni dejarla correr para siempre. Importado por nombre
        # para que el test lo pueda sustituir aquí y no en el `time` global,
        # que comparten asyncio y media docena de librerías.
        arranque = monotonic()
        limite_s = float(effective.tasas_fetch_minutos_max) * 60.0

        for indice, (fuente_id, url, institucion, hash_previo) in enumerate(fuentes):
            transcurrido = monotonic() - arranque
            if limite_s > 0 and transcurrido >= limite_s:
                # El techo se mira **antes** de empezar una fuente: cortar a
                # media descarga no ahorra lo ya gastado. Lo que queda sin leer
                # se lee en la corrida de dentro de 4 horas, que es justo lo
                # que hace tolerable cortar.
                reporte.cortada_por_tiempo = True
                log.warning(
                    "corrida_cortada_por_tiempo",
                    minutos=round(transcurrido / 60.0, 1),
                    sin_leer=len(fuentes) - indice,
                )
                break
            try:
                await _procesar(
                    reporte,
                    fetcher=fetcher,
                    cliente=cliente,
                    fuente_id=fuente_id,
                    url=url,
                    institucion=institucion,
                    hash_previo=hash_previo,
                )
            except ErrorPresupuestoAgotado:
                # El techo hizo su trabajo. No es un fallo de la corrida y no
                # tiene sentido seguir intentando con las que quedan.
                reporte.presupuesto_agotado = True
                log.warning("corrida_cortada_por_presupuesto", pendientes=url)
                break
            except Exception as exc:  # noqa: BLE001 — una fuente no tumba la corrida
                # Cualquier cosa inesperada procesando **una** fuente se anota
                # y se sigue con las demás. Lo aprendí de la peor manera: una
                # violación de clave única con Openbank se llevó por delante
                # las dos fuentes que faltaban, que no tenían nada que ver.
                reporte.fallidas += 1
                reporte.errores.append(
                    f"{institucion}: {type(exc).__name__}: {una_linea(str(exc), 200)}"
                )
                log.exception("fuente_fallida", institucion=institucion, url=url)
    finally:
        reporte.hosts_en_circuito = fetcher.hosts_en_circuito
        if propios:
            await fetcher.cerrar()
            await cliente.cerrar()

    log.info("corrida_tasas", **{k: v for k, v in reporte.como_metricas().items() if v})
    return reporte


async def _fuentes(solo_requieren_js: bool | None) -> list[tuple[int, str, str, str | None]]:
    """`(id, url, institución, hash previo)` de las fuentes activas.

    Se une con `instituciones` en vez de cargar una relación porque
    `FuenteTasas` no la declara: la tabla existe para el fetcher, no para
    navegar el dominio.
    """
    async with session_scope() as session:
        consulta = (
            select(FuenteTasas, Institucion.nombre)
            .join(Institucion, Institucion.id == FuenteTasas.institucion_id)
            .where(FuenteTasas.activa.is_(True))
            # Las fuentes de nivel 3 son portadas para el researcher, no
            # páginas de tasas: dárselas al extractor paga tokens por leer
            # marketing y, en el mejor de los casos, devuelve «vacía».
            .where(FuenteTasas.nivel <= 2)
            .order_by(FuenteTasas.id)
        )
        if solo_requieren_js is not None:
            consulta = consulta.where(FuenteTasas.requiere_js.is_(solo_requieren_js))
        filas = (await session.execute(consulta)).tuples().all()
        return [(f.id, f.url, nombre, f.ultimo_hash) for f, nombre in filas]


async def _procesar(
    reporte: ReporteCorrida,
    *,
    fetcher: Fetcher,
    cliente: ClienteLLM,
    fuente_id: int,
    url: str,
    institucion: str,
    hash_previo: str | None,
) -> None:
    try:
        descarga = await fetcher.descargar(url)
    except CadenaAgotada as exc:
        reporte.fallidas += 1
        reporte.errores.append(f"{institucion}: {exc}")
        await _anotar_fallo(reporte, fuente_id, institucion=institucion, error=str(exc))
        return

    if descarga is None:
        # 200 sin texto legible. Como desenlace de una corrida no es un fallo
        # —ni abre el circuito ni cuesta un token— pero repetido sí lo es: es
        # el caso de cetesdirecto, que lleva meses así sin que nada lo diga.
        reporte.vacias += 1
        await _anotar_fallo(
            reporte, fuente_id, institucion=institucion, error="la página no trae texto legible"
        )
        return

    reporte.leidas += 1
    # La descarga funcionó: la salud vuelve a cero **aquí**, antes de saber si
    # había tasas. `fallos_consecutivos` mide descargas, y ni un LLM caído ni
    # una página sin tasas son motivo para apagar una URL que responde bien.
    await _sanar(fuente_id)

    if hash_previo and hash_previo == descarga.hash_contenido:
        reporte.sin_cambios_en_la_pagina += 1
        log.info("pagina_sin_cambios", institucion=institucion, url=url)
        await _sellar(fuente_id, descarga.hash_contenido, con_tasas=False)
        return

    try:
        extraccion = await extraer(
            cliente, institucion=institucion, url=url, contenido=descarga.texto
        )
    except ErrorPresupuestoAgotado:
        raise
    except ErrorProveedor as exc:
        reporte.fallidas += 1
        reporte.errores.append(f"{institucion}: extracción fallida — {exc}")
        return

    if not extraccion.tasas:
        # Se descargó una página entera y no había ni una tasa. Seis fuentes
        # del catálogo están así porque apuntan a la portada en vez de a la
        # página de producto; sin nombrarlas aquí, la siguiente corrida las
        # salta por hash igual y quedan congeladas en silencio.
        reporte.sin_tasas.append(institucion)
        log.info("extraccion_sin_tasas", institucion=institucion, url=url)

    reporte.tasas_extraidas += len(extraccion.tasas)
    await _decidir(reporte, extraccion.tasas, institucion=institucion, url=url)
    # El sello va al final: si la extracción reventó, la próxima corrida tiene
    # que volver a intentarlo en vez de creer que ya se procesó.
    await _sellar(fuente_id, descarga.hash_contenido, con_tasas=bool(extraccion.tasas))


async def _decidir(
    reporte: ReporteCorrida,
    extraidas: list[TasaExtraida],
    *,
    institucion: str,
    url: str,
) -> None:
    if not extraidas:
        return

    async with session_scope() as session:
        productos = await _productos_de(session, institucion)
        vigentes = await tasas_vigentes_por_producto(
            session, [p.id for p in productos.values()], incluir_pendientes=True
        )

        for clave, grupo in _agrupar(extraidas).items():
            producto = productos.get(clave)
            # Varias entradas del mismo (tipo, plazo) son los **tramos por
            # monto** de un producto —Openbank publica 13 % hasta $30 000 y
            # 6.3 % de ahí en adelante— y se reconstruyen como UNA observación
            # con su escalera. Devuelve None cuando los montos no alcanzan
            # para saber dónde corta cada tramo (repetidos o ausentes): elegir
            # una de las tasas sería publicar el «hasta 13 %» que el extractor
            # tiene prohibido inventar, así que ese grupo sigue siendo hueco.
            escalera = reconstruir_escalera(grupo) if len(grupo) > 1 else None
            if producto is None or (len(grupo) > 1 and escalera is None):
                motivo = (
                    "plazo desconocido"
                    if producto is None
                    else "tramos ambiguos (montos repetidos o sin monto)"
                )
                for extraida in grupo:
                    reporte.huecos_catalogo.append(
                        HuecoCatalogo(
                            institucion=institucion,
                            producto=extraida.producto,
                            plazo_dias=extraida.plazo_dias,
                            tasa_nominal=extraida.tasa_nominal,
                            url=url,
                        ).como_dict()
                    )
                    log.info(
                        "hueco_catalogo",
                        institucion=institucion,
                        plazo_dias=extraida.plazo_dias,
                        tasa=str(extraida.tasa_nominal),
                        motivo=motivo,
                    )
                continue

            cabeza = escalera.cabeza if escalera is not None else grupo[0]
            candidata = vigentes.get(producto.id)
            vigente = candidata if candidata and candidata.estado is EstadoTasa.VIGENTE else None
            resultado = await revisar(
                session,
                cabeza,
                producto=producto,
                vigente=vigente,
                referencia=candidata if vigente is None else None,
                url=url,
                escalera=escalera,
            )
            # Una fila de reporte por observación: la escalera entera cuenta
            # una vez, no una por tramo.
            match resultado.decision:
                case Decision.PUBLICADA:
                    reporte.publicadas += 1
                case Decision.EN_REVISION:
                    reporte.en_revision += 1
                case Decision.SIN_CAMBIO:
                    reporte.sin_cambio += 1


def _agrupar(extraidas: list[TasaExtraida]) -> dict[tuple[str, int | None], list[TasaExtraida]]:
    """Las extracciones por `(tipo, plazo)`, en orden de aparición.

    Un grupo de una entrada es el caso normal; uno de varias son los tramos
    por monto de un mismo producto, que `reconstruir_escalera` decide si se
    pueden reconstruir o quedan como hueco.
    """
    grupos: dict[tuple[str, int | None], list[TasaExtraida]] = {}
    for extraida in extraidas:
        grupos.setdefault(_clave(extraida.tipo, extraida.plazo_dias), []).append(extraida)
    return grupos


async def _productos_de(session: Any, institucion: str) -> dict[tuple[str, int | None], Producto]:
    """Productos de esa institución, indexados por `(tipo, plazo)`.

    La clave es lo que la página publica —un tipo y un plazo— y no el nombre,
    que cada institución escribe distinto cada temporada.
    """
    filas = (
        (
            await session.execute(
                select(Producto)
                .join(Institucion, Institucion.id == Producto.institucion_id)
                .where(Institucion.nombre == institucion, Producto.activo.is_(True))
            )
        )
        .scalars()
        .all()
    )
    return {_clave(p.tipo, p.plazo_dias): p for p in filas}


def _clave(tipo: TipoProducto, plazo_dias: int | None) -> tuple[str, int | None]:
    return (tipo.value, plazo_dias)


async def _sellar(fuente_id: int, hash_contenido: str, *, con_tasas: bool) -> None:
    """Marca la descarga, y el éxito **sólo** si de verdad salieron tasas.

    `ultima_extraccion_at` dice «se descargó»; `ultimo_exito_at`, «sirvió de
    algo». Confundirlas es lo que dejó seis portadas sin tasas indistinguibles
    de seis lecturas buenas.
    """
    async with session_scope() as session:
        fuente = await session.get(FuenteTasas, fuente_id)
        if fuente is not None:
            fuente.ultimo_hash = hash_contenido
            fuente.ultima_extraccion_at = datetime.now(UTC)
            if con_tasas:
                fuente.ultimo_exito_at = datetime.now(UTC)


async def _sanar(fuente_id: int) -> None:
    """La descarga funcionó: se olvidan los fallos y el último error."""
    async with session_scope() as session:
        fuente = await session.get(FuenteTasas, fuente_id)
        if fuente is not None and (fuente.fallos_consecutivos or fuente.ultimo_error):
            fuente.fallos_consecutivos = 0
            fuente.ultimo_error = None


async def _anotar_fallo(
    reporte: ReporteCorrida, fuente_id: int, *, institucion: str, error: str
) -> None:
    """Suma un fallo a la fuente y la apaga si ya son demasiados.

    Apagarla es lo que impide que un dominio muerto siga costando una cadena de
    transportes entera —Chromium incluido— seis veces al día durante meses.
    Volver a encenderla es humano a propósito: si se recuperara sola, el
    problema volvería a ser invisible, que es exactamente de donde venimos.
    """
    umbral = int(effective.fetch_fallos_para_pausar)
    async with session_scope() as session:
        fuente = await session.get(FuenteTasas, fuente_id)
        if fuente is None:
            return
        fuente.fallos_consecutivos += 1
        # De una línea: `cli fuentes list` lo imprime dentro de una tabla y un
        # salto de línea del «Call log» de Playwright la parte en dos.
        fuente.ultimo_error = una_linea(error, 300)
        if not fuente.activa or umbral <= 0 or fuente.fallos_consecutivos < umbral:
            return
        fuente.activa = False
        fuente.pausada_motivo = (
            f"{fuente.fallos_consecutivos} fallos seguidos: {una_linea(error, 200)}"
        )
        reporte.fuentes_pausadas.append(f"{institucion} — {fuente.url}")
        log.warning(
            "fuente_pausada",
            institucion=institucion,
            url=fuente.url,
            fallos=fuente.fallos_consecutivos,
            error=una_linea(error, 200),
        )


__all__ = ["ReporteCorrida", "correr"]
