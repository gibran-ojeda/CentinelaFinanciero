"""Cola de revisión desde la terminal.

Es la cara con la que se resuelve la cola en minutos (decisión D4: CLI primero,
mini-UI sólo si el volumen lo justifica). La lógica de aprobar y rechazar no
está aquí sino en `api.services.revisiones`, que es la misma que usan los
endpoints admin — dos implementaciones acabarían discrepando.

Lo que aporta este módulo es la **presentación**: quien revisa necesita ver de
un vistazo qué institución, qué producto, de cuánto a cuánto y por qué, con la
URL para comprobarlo. Una cola que enseñe ids obliga a buscar y deja de
resolverse en minutos.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select

from api.services import cache, revisiones
from core.db import session_scope
from core.logging import get_logger
from domain.enums import EstadoRevision
from domain.orm import JobRun

log = get_logger(__name__)


def _num(valor: Decimal) -> str:
    """Sin los ceros de relleno del tipo `Porcentaje`, que guarda cuatro.

    `7.8900` es exacto y `7.89` es legible. En una tabla que alguien recorre a
    ojo cada semana, los dos ceros de más sólo hacen ruido. Nunca por debajo de
    dos decimales, que es como se leen las tasas.
    """
    recortado = valor.normalize()
    if -recortado.as_tuple().exponent < 2:  # type: ignore[operator]
        recortado = recortado.quantize(Decimal("0.01"))
    return f"{recortado:f}"


def _flecha(anterior: Decimal | None, nuevo: Decimal) -> str:
    if anterior is None:
        return f"→ {_num(nuevo)}%"
    delta = nuevo - anterior
    signo = "+" if delta > 0 else ""
    return f"{_num(anterior)}% → {_num(nuevo)}%  ({signo}{_num(delta)} pp)"


async def listar(estado: EstadoRevision = EstadoRevision.PENDIENTE) -> str:
    """La cola, más los huecos de catálogo de las corridas recientes."""
    async with session_scope() as session:
        filas = await revisiones.listar(session, estado=estado)
        huecos = await _huecos_recientes(session)

    lineas: list[str] = []
    if not filas:
        lineas.append(f"  No hay revisiones en estado {estado.value}.")
    else:
        institucion_actual = ""
        for fila in filas:
            if fila.institucion != institucion_actual:
                institucion_actual = fila.institucion
                lineas.append(f"\n  {institucion_actual}")
            plazo = f"{fila.plazo_dias}d" if fila.plazo_dias else "vista"
            lineas.append(
                f"    [{fila.id:>4}] {fila.producto} ({plazo})  "
                f"{_flecha(fila.valor_anterior, fila.valor_nuevo)}"
            )
            lineas.append(f"           {fila.motivo}")
            if fila.fuente_url:
                lineas.append(f"           {fila.fuente_url}")
        lineas.append(
            f"\n  {len(filas)} pendientes. "
            f"Aprobar: python -m cli revisiones approve <id> --revisor <quien>"
        )

    if huecos:
        lineas.append("\n  ── Huecos de catálogo (corridas recientes) ──")
        lineas.append("  Estas instituciones publican plazos que el catálogo no tiene. No son")
        lineas.append(
            "  revisiones: se cierran dando de alta el producto en seeds/productos.yaml."
        )
        for hueco in huecos:
            etiqueta = f"{hueco['plazo_dias']}d" if hueco.get("plazo_dias") else "vista"
            lineas.append(
                f"    {hueco.get('institucion', '?'):<24} {etiqueta:>7}  "
                f"{hueco.get('tasa_nominal', '?')}%  {hueco.get('producto', '')}"
            )

    return "\n".join(lineas)


async def resolver(
    revision_id: int, *, aprobar: bool, revisor: str, comentario: str | None
) -> str:
    async with session_scope() as session:
        try:
            revision = await revisiones.resolver(
                session, revision_id, aprobar=aprobar, revisor=revisor, comentario=comentario
            )
        except (revisiones.RevisionNoEncontrada, revisiones.RevisionYaResuelta) as exc:
            raise SystemExit(f"Error: {exc}") from exc
        estado = revision.estado.value

    # Aprobar publica una tasa, así que el comparador tiene que dejar de servir
    # lo que tenía cacheado.
    if aprobar:
        await cache.invalidar()

    return f"  Revisión {revision_id}: {estado} por {revisor}."


async def _huecos_recientes(session: Any) -> list[dict[str, Any]]:
    """Los huecos de catálogo de las corridas recientes, deduplicados.

    Salen de `job_runs.metricas` y no de una tabla propia: son un hallazgo de
    una corrida concreta, no un estado del sistema, y guardarlos aparte
    obligaría a mantenerlos sincronizados con un catálogo que cambia.

    Pero «la última corrida» a secas no bastaba: la pasada del VPS, la local
    con navegador y la del researcher son corridas distintas —ids distintos—
    y cada una ve fuentes que las otras no. Se agregan las últimas de los
    tres ids y **en cualquier estado**, porque los huecos de una corrida
    fallida son igual de reales que los de una exitosa. Gana la mención más
    reciente de cada hueco.
    """
    corridas = (
        (
            await session.execute(
                select(JobRun)
                .where(
                    JobRun.job_id.in_(
                        (
                            "tasas_fetch_rapido",
                            "tasas_fetch_navegador",
                            # El id que tuvo el fetch antes de partirse en dos.
                            "tasas_fetch_dirigido",
                            "tasas_fetch_manual",
                            "tasas_research_abierta",
                        )
                    )
                )
                .order_by(desc(JobRun.id))
                .limit(15)
            )
        )
        .scalars()
        .all()
    )

    vistos: set[tuple[Any, Any, Any]] = set()
    huecos: list[dict[str, Any]] = []
    for corrida in corridas:  # de la más reciente a la más vieja
        crudos = (corrida.metricas or {}).get("huecos_catalogo")
        if not isinstance(crudos, list):
            continue
        for hueco in crudos:
            clave = (hueco.get("institucion"), hueco.get("producto"), hueco.get("plazo_dias"))
            if clave in vistos:
                continue
            vistos.add(clave)
            huecos.append(hueco)
    return huecos


__all__ = ["listar", "resolver"]
