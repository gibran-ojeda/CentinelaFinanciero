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

from datetime import UTC, datetime

from sqlalchemy import select

from core.db import session_scope
from core.logging import get_logger
from domain.orm import FuenteTasas, Institucion

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


__all__ = ["cambiar_url", "listar", "pausar", "reanudar"]
