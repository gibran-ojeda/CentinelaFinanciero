"""Diagnóstico y reparación del catálogo de fuentes, desde la terminal.

Existe por lo que pasó el 2026-08-02: cinco de las catorce fuentes de nivel 2
llevaban semanas rotas —dos dominios muertos, un 403 permanente, un host sin
DNS y una página que responde 200 sin texto— y no había ninguna superficie
donde verlo. `cli tasas pendientes` listaba sus URLs **como si funcionaran**, y
la única forma de corregir una era editar `seeds/fuentes_tasas.yaml`, hacer
commit y desplegar.

Corregir la URL aquí y no en el YAML no es una vía paralela: es la reparación
urgente. El YAML sigue siendo la fuente de verdad —el siguiente `cli seed` la
impone— y precisamente por eso `url` avisa de que el cambio es provisional
hasta que alguien lo lleve al repo.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from core.db import session_scope
from core.logging import get_logger
from core.settings import settings
from domain.orm import FuenteTasas, Institucion
from llm.client import ClienteLLM

log = get_logger(__name__)


def _edad(momento: datetime | None) -> str:
    """«hace 3 d», o «nunca». Un timestamp ISO no se lee de un vistazo."""
    if momento is None:
        return "nunca"
    dias = (datetime.now(UTC) - momento).days
    if dias >= 1:
        return f"hace {dias} d"
    return "hoy"


async def listar(*, solo_rotas: bool = False) -> str:
    """Cada fuente con su salud, agrupada por institución."""
    async with session_scope() as session:
        filas = (
            (
                await session.execute(
                    select(FuenteTasas, Institucion.nombre)
                    .join(Institucion, Institucion.id == FuenteTasas.institucion_id)
                    .order_by(Institucion.nombre, FuenteTasas.id)
                )
            )
            .tuples()
            .all()
        )

    lineas: list[str] = []
    rotas = 0
    sin_producir = 0
    institucion_actual = ""
    for fuente, institucion in filas:
        # «Rota» es cualquiera de las dos averías del incidente: la que falla y
        # la que se descarga bien sin publicar nunca una tasa. La segunda no
        # deja ni un error y era la más difícil de ver.
        nunca_produjo = fuente.ultimo_exito_at is None
        problematica = bool(fuente.fallos_consecutivos) or not fuente.activa or nunca_produjo
        if problematica:
            rotas += 1
        if nunca_produjo and fuente.activa:
            sin_producir += 1
        if solo_rotas and not problematica:
            continue

        if institucion != institucion_actual:
            institucion_actual = institucion
            lineas.append(f"\n  {institucion_actual}")

        if not fuente.activa:
            estado = "PAUSADA"
        elif fuente.fallos_consecutivos:
            estado = f"{fuente.fallos_consecutivos} fallos"
        else:
            estado = "ok"
        marca = "js" if fuente.requiere_js else "  "
        lineas.append(
            f"    [{fuente.id:>3}] n{fuente.nivel} {marca} {estado:<10} "
            f"último dato: {_edad(fuente.ultimo_exito_at):<10} {fuente.url}"
        )
        if fuente.pausada_motivo:
            lineas.append(f"          pausada: {fuente.pausada_motivo}")
        elif fuente.ultimo_error:
            lineas.append(f"          error:   {fuente.ultimo_error[:150]}")

    if not lineas:
        return "  Ninguna fuente con problemas." if solo_rotas else "  No hay fuentes cargadas."

    lineas.append(f"\n  {len(filas)} fuentes, {rotas} con algo que mirar.")
    if sin_producir:
        lineas.append(
            f"  {sin_producir} activas nunca han producido una tasa: casi siempre es una"
        )
        lineas.append("  URL que apunta a la portada y no a la página que publica las tasas.")
    lineas.append("  Reparar: python -m cli fuentes url <id> <url>  |  ... reanudar <id>")
    return "\n".join(lineas)


@dataclass(frozen=True, slots=True)
class _Medida:
    """Lo que sacó un transporte de una URL."""

    caracteres: int | None = None
    tasas: int | None = None
    error: str | None = None

    def render(self) -> str:
        if self.error is not None:
            return self.error
        if self.caracteres is None:
            return "vacía"
        cuenta = f"{self.caracteres} caracteres"
        return cuenta if self.tasas is None else f"{cuenta}, {self.tasas} tasas"


async def probar(fuente_id: int | None = None, *, extraer_tasas: bool = False) -> str:
    """Descarga cada fuente con **cada transporte por separado**.

    La sonda que faltaba. `requiere_js` decide desde qué job se lee una fuente,
    y hasta ahora nadie la había medido: el YAML dice, literalmente, que las
    nuevas van marcadas `true` «por prudencia». Repartir dos cadencias sobre una
    suposición es construir encima de nada.

    `cli tasas fetch --solo-navegador` no sirve para esto: filtra **qué
    fuentes** corren, no con qué se descargan.

    **Los caracteres solos no deciden.** Una tabla de tasas añade unas decenas
    de caracteres a una página de marketing que ya tiene cientos, así que
    cualquier umbral sobre el tamaño sería adivinar. Con `--extraer` se le pasa
    cada texto al extractor y se cuenta lo único que importa: si de ahí salen
    tasas. Cuesta dos llamadas al modelo por fuente y es lo que da un veredicto
    en vez de una pista.

    No toca la base: ni sella hash, ni cuenta fallos, ni pausa nada. Se puede
    correr contra producción sin consecuencias.
    """
    from rates_agent.fetcher import ErrorDescarga, Fetcher, TransporteHttpx, una_linea
    from rates_agent.navegador import TransporteNavegador

    async with session_scope() as session:
        consulta = (
            select(FuenteTasas, Institucion.nombre)
            .join(Institucion, Institucion.id == FuenteTasas.institucion_id)
            .where(FuenteTasas.nivel <= 2)
            .order_by(Institucion.nombre, FuenteTasas.id)
        )
        if fuente_id is not None:
            consulta = consulta.where(FuenteTasas.id == fuente_id)
        filas = [
            (f.id, f.url, nombre, f.requiere_js, f.activa)
            for f, nombre in (await session.execute(consulta)).tuples().all()
        ]

    if not filas:
        raise SystemExit(
            f"Error: no existe la fuente {fuente_id}." if fuente_id else "  No hay fuentes."
        )

    agente = settings.fetch_user_agent
    cliente = ClienteLLM() if extraer_tasas else None
    lineas: list[str] = []
    desacuerdos = 0
    try:
        for id_, url, institucion, requiere_js, activa in filas:
            medidas: dict[str, _Medida] = {}
            # El mismo User-Agent en las dos vías: se compara la página, no el bot.
            for transporte in (
                TransporteHttpx(user_agent=agente),
                TransporteNavegador(user_agent=agente),
            ):
                # Un fetcher por transporte y sin backoff: se mide qué saca cada
                # vía, no cómo se recupera la cadena de un sitio caído.
                solo = Fetcher([transporte], esperas_backoff_s=())
                try:
                    descarga = await solo.descargar(url)
                except ErrorDescarga as exc:
                    medidas[transporte.nombre] = _Medida(error=una_linea(str(exc), 60))
                else:
                    medidas[transporte.nombre] = await _medir(
                        descarga, cliente, institucion=institucion, url=url
                    )
                finally:
                    await solo.cerrar()

            veredicto = _veredicto(medidas)
            marca = ""
            if veredicto is not None and veredicto != requiere_js:
                desacuerdos += 1
                marca = "  ← la marca dice lo contrario"
            lineas.append(f"\n  {institucion}  [{id_}]{'' if activa else '  (pausada)'}")
            lineas.append(f"    {url}")
            for nombre, medida in medidas.items():
                lineas.append(f"      {nombre:<10} {medida.render()}")
            if veredicto is None:
                lineas.append("      → sin veredicto: mira las dos medidas y decide")
            else:
                lineas.append(f"      → requiere_js: {veredicto}{marca}")
    finally:
        if cliente is not None:
            await cliente.cerrar()

    if not extraer_tasas:
        lineas.append("\n  Sin --extraer sólo se miden caracteres, y eso no basta para decidir:")
        lineas.append("  una tabla de tasas pesa poco al lado del marketing de la página.")
    elif desacuerdos:
        lineas.append(
            f"\n  {desacuerdos} fuentes con la marca al revés de lo medido. "
            "Corregir en seeds/fuentes_tasas.yaml."
        )
    else:
        lineas.append("\n  La marca coincide con lo medido en todas.")
    return "\n".join(lineas)


async def _medir(
    descarga: object | None, cliente: ClienteLLM | None, *, institucion: str, url: str
) -> _Medida:
    """Caracteres de la descarga, y las tasas que saca de ella el extractor."""
    from rates_agent.extractor import extraer

    if descarga is None:
        return _Medida()
    texto: str = descarga.texto  # type: ignore[attr-defined]
    if cliente is None:
        return _Medida(caracteres=len(texto))
    try:
        extraccion = await extraer(cliente, institucion=institucion, url=url, contenido=texto)
    except Exception as exc:  # noqa: BLE001 — la sonda reporta, no decide
        return _Medida(caracteres=len(texto), error=f"{len(texto)} caracteres, extracción: {exc}")
    return _Medida(caracteres=len(texto), tasas=len(extraccion.tasas))


def _veredicto(medidas: dict[str, _Medida]) -> bool | None:
    """`True` si sólo el navegador consigue tasas.

    Sin haber extraído no hay veredicto: los caracteres dicen cuánto texto
    trajo cada vía, no si el texto contiene lo que buscamos. Y si ninguna saca
    tasas, el problema es otro —la URL, un WAF, el umbral— y decidir
    `requiere_js` sobre eso sería inventarse el dato.
    """
    plano = medidas.get("httpx", _Medida()).tasas
    navegador = medidas.get("navegador", _Medida()).tasas
    if plano is None and navegador is None:
        return None
    if not (plano or navegador):
        return None
    return not plano


async def _cambiar_activa(fuente_id: int, *, activa: bool, motivo: str | None) -> str:
    async with session_scope() as session:
        fuente = await session.get(FuenteTasas, fuente_id)
        if fuente is None:
            raise SystemExit(f"Error: no existe la fuente {fuente_id}.")
        fuente.activa = activa
        if activa:
            # Reanudar es olvidar: si el contador siguiera donde estaba, el
            # primer fallo la volvería a apagar y la reanudación no habría
            # servido para nada.
            fuente.fallos_consecutivos = 0
            fuente.ultimo_error = None
            fuente.pausada_motivo = None
            verbo = "reanudada"
        else:
            fuente.pausada_motivo = motivo or "pausada a mano"
            verbo = "pausada"
        url = fuente.url

    log.info("fuente_estado_cambiado", fuente_id=fuente_id, activa=activa, motivo=motivo)
    return f"  Fuente {fuente_id} {verbo}: {url}"


async def pausar(fuente_id: int, *, motivo: str) -> str:
    return await _cambiar_activa(fuente_id, activa=False, motivo=motivo)


async def reanudar(fuente_id: int) -> str:
    return await _cambiar_activa(fuente_id, activa=True, motivo=None)


async def cambiar_url(fuente_id: int, url: str) -> str:
    """Corrige la URL **en su sitio**, sin crear una fila nueva.

    Editar el YAML inserta otra fuente y deja la muerta viva y activa, porque
    la clave del upsert incluye la URL. Aquí se sustituye la que hay, se olvida
    el hash —el contenido de la página nueva no tiene nada que ver con el de la
    vieja— y se reinicia la salud, que era del sitio anterior.
    """
    if not url.startswith(("http://", "https://")):
        raise SystemExit(f"Error: '{url}' no parece una URL.")

    async with session_scope() as session:
        fuente = await session.get(FuenteTasas, fuente_id)
        if fuente is None:
            raise SystemExit(f"Error: no existe la fuente {fuente_id}.")
        anterior = fuente.url
        if url != anterior:
            choque = await session.scalar(
                select(FuenteTasas).where(
                    FuenteTasas.institucion_id == fuente.institucion_id,
                    FuenteTasas.url == url,
                )
            )
            if choque is not None:
                raise SystemExit(
                    f"Error: esa institución ya tiene la fuente {choque.id} con esa URL."
                )
        fuente.url = url
        fuente.ultimo_hash = None
        fuente.ultimo_exito_at = None
        fuente.fallos_consecutivos = 0
        fuente.ultimo_error = None
        fuente.pausada_motivo = None
        fuente.activa = True

    log.info("fuente_url_cambiada", fuente_id=fuente_id, anterior=anterior, nueva=url)
    return (
        f"  Fuente {fuente_id}: {anterior}\n"
        f"                  → {url}\n"
        "  Provisional hasta que el cambio llegue a seeds/fuentes_tasas.yaml:\n"
        "  el siguiente `cli seed` impone lo que diga el repo."
    )


__all__ = ["cambiar_url", "listar", "pausar", "probar", "reanudar"]
