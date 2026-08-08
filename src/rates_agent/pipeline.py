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

from collections import Counter
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
from rates_agent.escalera import colapsar_por_condicion, reconstruir_escalera
from rates_agent.extractor import TasaExtraida, extraer
from rates_agent.fetcher import CadenaAgotada, Fetcher, SinTransporteCapaz, una_linea
from rates_agent.reviewer import Decision, HuecoCatalogo, revisar

log = get_logger(__name__)

#: Tope de lo que se guarda de los reclamos ambiguos de una fuente. Acaba en el
#: motivo de una bandera, que se lee en una pastilla y en un `title`: más largo
#: no informa mejor.
TOPE_AMBIGUEDAD = 400


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
    #: Instituciones que anuncian una tasa sin decir a qué corresponde. Va
    #: aparte de `sin_tasas` porque son problemas distintos: una URL mal
    #: apuntada se arregla cambiando la URL, y esto no se arregla desde aquí.
    ambiguas: list[str] = field(default_factory=list)
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
            "ambiguas": self.ambiguas,
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
        if self.ambiguas:
            lineas.append(f"  anuncian sin concretar  {', '.join(self.ambiguas)}")
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
    forzar: bool = False,
) -> ReporteCorrida:
    """Recorre las fuentes activas y deja el resultado en la base.

    Args:
        solo_requieren_js: `True` corre sólo las páginas que necesitan
            navegador; `False`, sólo las que no. `None` (por defecto), todas.
            Es el filtro de depuración de la CLI — repetir una mitad de la
            corrida sin pagar la otra — y el que usa el repliegue
            `tasas_fetch_solo_sin_js`.
        forzar: extrae aunque la página no haya cambiado. El cortocircuito por
            hash asume que lo que cambia es la página, y hay un caso en que no:
            **cuando cambia el catálogo**. El 2026-08-07 se dieron de alta los
            nueve productos que Klar y Hey publican de verdad, y las corridas
            siguientes los habrían dejado sin tasa para siempre —el hash de
            esas dos páginas estaba sellado desde antes— hasta que a Klar se le
            ocurriera editar su web. Cuesta tokens: es un override manual, no
            algo que el job programado use nunca.
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

        for indice, (fuente_id, url, institucion, hash_previo, requiere_js) in enumerate(fuentes):
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
                    hash_previo=None if forzar else hash_previo,
                    requiere_js=requiere_js,
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


async def _fuentes(
    solo_requieren_js: bool | None,
) -> list[tuple[int, str, str, str | None, bool]]:
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
        return [(f.id, f.url, nombre, f.ultimo_hash, f.requiere_js) for f, nombre in filas]


async def _procesar(
    reporte: ReporteCorrida,
    *,
    fetcher: Fetcher,
    cliente: ClienteLLM,
    fuente_id: int,
    url: str,
    institucion: str,
    hash_previo: str | None,
    requiere_js: bool = False,
) -> None:
    try:
        descarga = await fetcher.descargar(url, requiere_js=requiere_js)
    except SinTransporteCapaz as exc:
        # La fuente pide navegador y esta corrida no lo lleva. Es un fallo del
        # reparto entre jobs, no de la fuente: se anota y **no** cuenta contra
        # su salud, o el scheduler acabaría autopausando páginas que funcionan.
        reporte.errores.append(f"{institucion}: {exc}")
        log.warning("fuente_sin_transporte", institucion=institucion, url=url)
        return
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

    if extraccion.ambiguas:
        # No es un fallo: la página se leyó bien y el extractor obedeció la
        # regla 1. Es la institución la que no dice a qué corresponde su
        # número, y eso acaba en una bandera.
        reporte.ambiguas.append(institucion)
        log.info(
            "tasas_ambiguas", institucion=institucion, url=url, cuantas=len(extraccion.ambiguas)
        )

    reporte.tasas_extraidas += len(extraccion.tasas)
    await _decidir(reporte, extraccion.tasas, institucion=institucion, url=url)
    # El sello va al final: si la extracción reventó, la próxima corrida tiene
    # que volver a intentarlo en vez de creer que ya se procesó.
    await _sellar(
        fuente_id,
        descarga.hash_contenido,
        con_tasas=bool(extraccion.tasas),
        ambiguas=extraccion.ambiguas,
    )


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
        catalogo = await _productos_de(session, institucion)
        vigentes = await tasas_vigentes_por_producto(
            session, [p.id for p in catalogo.todos()], incluir_pendientes=True
        )

        grupos = _agrupar(extraidas)
        # Cuántos productos publicados caen en la misma casilla del catálogo.
        # Más de uno significa que la página ofrece más de lo que la casilla
        # puede distinguir, y ahí sólo el nombre publicado desempata.
        por_casilla = Counter(clave[:2] for clave in grupos)

        for clave, grupo in grupos.items():
            casilla = clave[:2]
            producto = catalogo.resolver(clave, grupos_en_la_casilla=por_casilla[casilla])
            colisionan = producto is None and por_casilla[casilla] > 1
            # Varias entradas del mismo (tipo, plazo) son los **tramos por
            # monto** de un producto —Openbank publica 13 % hasta $30 000 y
            # 6.3 % de ahí en adelante— y se reconstruyen como UNA observación
            # con su escalera. Devuelve None cuando los montos no alcanzan
            # para saber dónde corta cada tramo (repetidos o ausentes): elegir
            # una de las tasas sería publicar el «hasta 13 %» que el extractor
            # tiene prohibido inventar, así que ese grupo sigue siendo hueco.
            #
            # Se llama también con una sola entrada, que antes ni se intentaba:
            # una que declara `monto_maximo` —«15 % en tus primeros $25 000»—
            # también es una escalera, y sin esto su tope se perdía y la tabla
            # prometía esa tasa sobre cualquier saldo. Sin tope sigue
            # devolviendo None y la observación viaja plana, como siempre.
            escalera = reconstruir_escalera(grupo) if producto is not None else None
            # No es escalera y el grupo trae varias: puede que se diferencien
            # por condición y no por monto —Hey publica 4.00 % como Cliente Hey
            # y 7.50 % siendo Fan Hey—. Ahí se colapsa en la más baja con el
            # resto en `condiciones` en vez de tirar el grupo entero.
            colapsada = (
                colapsar_por_condicion(grupo)
                if producto is not None and escalera is None and len(grupo) > 1
                else None
            )
            if producto is None or (len(grupo) > 1 and escalera is None and colapsada is None):
                motivo = (
                    "el catálogo no distingue los varios productos de esta casilla; "
                    "se arregla con `nombre_publicado` en seeds/productos.yaml"
                    if colisionan
                    else (
                        "plazo desconocido"
                        if producto is None
                        else "tramos ambiguos (montos repetidos o sin monto)"
                    )
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

            cabeza = escalera.cabeza if escalera is not None else (colapsada or grupo[0])
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


def _agrupar(extraidas: list[TasaExtraida]) -> dict[_ClaveGrupo, list[TasaExtraida]]:
    """Las extracciones por producto publicado, en orden de aparición.

    Un grupo de una entrada es el caso normal; uno de varias son los tramos
    por monto de un mismo producto, que `reconstruir_escalera` decide si se
    pueden reconstruir o quedan como hueco.

    **La clave incluye el nombre del producto**, no sólo `(tipo, plazo)`. Klar
    publica en la misma página «Cuenta» al 3 % e «Inversión Flexible» al 6 %,
    las dos a la vista: con la clave vieja caían en el mismo grupo y salía la
    escalera «$0–$100: 3 % · $100 en adelante: 6 %», que Klar no ofrece. Los
    $100 son el mínimo de contratación del segundo producto, no el piso de un
    tramo, y el dinero por encima de $100 no pasa a rendir 6 % solo: hay que
    moverlo a otro producto. El modelo sí los distinguía —les puso nombres
    distintos— y esta función tiraba el nombre.

    Los tramos de un mismo producto sí comparten nombre, y la regla 4 del
    prompt lo pide explícitamente. Si aun así llegaran con nombres distintos,
    el grupo se parte y sale hueco de catálogo: es el fallo seguro de los dos.
    """
    grupos: dict[_ClaveGrupo, list[TasaExtraida]] = {}
    for extraida in extraidas:
        clave = (*_clave(extraida.tipo, extraida.plazo_dias), _nombre(extraida.producto))
        grupos.setdefault(clave, []).append(extraida)
    return grupos


def _nombre(producto: str) -> str:
    """El nombre publicado, normalizado para compararlo.

    Sin mayúsculas ni espacios de más: «Cuenta de Ahorro » y «cuenta de ahorro»
    son el mismo producto, y partirlos por eso convertiría una escalera buena
    en un hueco.
    """
    return " ".join(producto.split()).casefold()


def _encabeza(declarado: str, leido: str) -> bool:
    """El nombre declarado en el catálogo encabeza al que trajo la página.

    Los dos vienen ya normalizados por `_nombre`, así que comparar en crudo
    basta. El espacio del prefijo no es adorno: sin él «Cuenta» se quedaría con
    «Cuentahorro».
    """
    return leido == declarado or leido.startswith(f"{declarado} ")


@dataclass(frozen=True, slots=True)
class _Catalogo:
    """Los productos de una institución, con las dos formas de encontrarlos.

    `por_casilla` es la de siempre: `(tipo, plazo)`, que es lo que la página
    publica de cada tasa. `por_nombre` sólo tiene los que declaran
    `nombre_publicado`, y es la que desempata cuando una casilla aloja a más de
    uno — Klar publica «Cuenta» e «Inversión Flexible», las dos a la vista.
    """

    por_casilla: dict[_ClaveCatalogo, Producto]
    por_nombre: dict[_ClaveGrupo, Producto]

    def resolver(self, clave: _ClaveGrupo, *, grupos_en_la_casilla: int) -> Producto | None:
        """El producto del catálogo para un grupo de extracciones, si lo hay.

        El nombre manda sobre la casilla: es el dato más específico, y cuando
        está declarado es porque la casilla sola no bastaba.

        `grupos_en_la_casilla` es cuántos productos distintos publica la página
        en esa misma casilla. Con más de uno y sin nombre que lo resuelva no se
        devuelve nada, **aunque el catálogo tenga un producto ahí**: tenerlo no
        dice cuál de los publicados es, y dárselo a los dos es exactamente lo
        que fabricó la escalera de Klar.
        """
        if (por_nombre := self._por_nombre(clave)) is not None:
            return por_nombre
        if grupos_en_la_casilla > 1:
            return None
        return self.por_casilla.get(clave[:2])

    def _por_nombre(self, clave: _ClaveGrupo) -> Producto | None:
        """El producto cuyo `nombre_publicado` **encabeza** el nombre leído.

        Por prefijo y no por igualdad porque la página cuelga el plazo del
        nombre. Hey publica el mismo pagaré como «Cliente Hey 7 días» y
        «Cliente Hey 28 días», y el catálogo ya distingue esos dos por la
        casilla: pedirle además un `nombre_publicado` por plazo sería repetir
        en el nombre lo que el plazo ya dice, y bastaría con que el modelo
        escribiera «7 dias» sin acento para perderlo todo.

        El corte es en frontera de palabra —el leído es el declarado, o el
        declarado seguido de un espacio— para que «Cuenta» no se quede con
        «Cuentahorro», que sería otro producto.

        Gana el declarado más largo: con «Cliente Hey» y «Cliente Hey Plus» en
        el catálogo, «cliente hey plus 7 días» es del segundo. No hace falta
        desempatar: dos nombres declarados distintos no pueden encabezar el
        mismo leído con la misma longitud, serían el mismo nombre.
        """
        casilla, leido = clave[:2], clave[2]
        candidatos = [
            (clave_catalogo[2], producto)
            for clave_catalogo, producto in self.por_nombre.items()
            if clave_catalogo[:2] == casilla and _encabeza(clave_catalogo[2], leido)
        ]
        if not candidatos:
            return None
        return max(candidatos, key=lambda par: len(par[0]))[1]

    def todos(self) -> list[Producto]:
        vistos = {p.id: p for p in self.por_casilla.values()}
        vistos.update({p.id: p for p in self.por_nombre.values()})
        return list(vistos.values())


async def _productos_de(session: Any, institucion: str) -> _Catalogo:
    """Productos activos de esa institución, listos para resolver.

    La casilla es lo que la página publica de cada tasa —un tipo y un plazo— y
    no el nombre, que cada institución escribe distinto cada temporada. Por eso
    el nombre es opcional: se declara sólo donde la casilla no alcanza.

    Dos productos en la misma casilla y **ninguno con nombre declarado** se
    descartan los dos. Antes el diccionario se quedaba con el último y la
    corrida atribuía la tasa a un producto elegido por el orden de la consulta.
    Sin producto no hay observación y sale hueco, que es el fallo seguro — y el
    motivo del hueco dice cómo arreglarlo.
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

    por_nombre: dict[_ClaveGrupo, Producto] = {}
    agrupados: dict[_ClaveCatalogo, list[Producto]] = {}
    for producto in filas:
        casilla = _clave(producto.tipo, producto.plazo_dias)
        agrupados.setdefault(casilla, []).append(producto)
        if producto.nombre_publicado:
            por_nombre[(*casilla, _nombre(producto.nombre_publicado))] = producto

    por_casilla: dict[_ClaveCatalogo, Producto] = {}
    for casilla, productos in agrupados.items():
        if len(productos) > 1:
            # No es un error si los nombres los distinguen: `resolver` los
            # encontrará por ahí. Se avisa igual de los que no lo declaran,
            # porque ésos sí quedan inalcanzables.
            sin_nombre = [p.slug for p in productos if not p.nombre_publicado]
            if sin_nombre:
                log.warning(
                    "catalogo_ambiguo",
                    institucion=institucion,
                    tipo=casilla[0],
                    plazo_dias=casilla[1],
                    sin_nombre_publicado=sin_nombre,
                )
            continue
        por_casilla[casilla] = productos[0]
    return _Catalogo(por_casilla=por_casilla, por_nombre=por_nombre)


#: `(tipo, plazo)` identifica la casilla del **catálogo**; el nombre del
#: producto identifica lo que la **página** publica. Son cosas distintas, y
#: confundirlas es lo que fabricó la escalera de Klar.
_ClaveCatalogo = tuple[str, int | None]
_ClaveGrupo = tuple[str, int | None, str]


def _clave(tipo: TipoProducto, plazo_dias: int | None) -> _ClaveCatalogo:
    return (tipo.value, plazo_dias)


async def _sellar(
    fuente_id: int,
    hash_contenido: str,
    *,
    con_tasas: bool,
    ambiguas: list[str] | None = None,
) -> None:
    """Marca la descarga, y el éxito **sólo** si de verdad salieron tasas.

    `ultima_extraccion_at` dice «se descargó»; `ultimo_exito_at`, «sirvió de
    algo». Confundirlas es lo que dejó seis portadas sin tasas indistinguibles
    de seis lecturas buenas.

    `ambiguas` en `None` significa «no hubo lectura»: la página no cambió y se
    saltó la extracción, así que lo que declara sigue siendo lo de la vez
    anterior. Una lista —aunque venga vacía— sí sobrescribe.
    """
    async with session_scope() as session:
        fuente = await session.get(FuenteTasas, fuente_id)
        if fuente is None:
            return
        fuente.ultimo_hash = hash_contenido
        fuente.ultima_extraccion_at = datetime.now(UTC)
        if con_tasas:
            fuente.ultimo_exito_at = datetime.now(UTC)
        if ambiguas is not None:
            fuente.ultima_ambiguedad = (
                una_linea(" · ".join(ambiguas), TOPE_AMBIGUEDAD) if ambiguas else None
            )
            fuente.ultima_ambiguedad_at = datetime.now(UTC) if ambiguas else None


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
