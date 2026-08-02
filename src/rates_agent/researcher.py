"""Nivel 3: búsqueda abierta con tool-use, y la invariante que la hace usable.

El nivel 2 —fetch dirigido sobre una URL curada— resuelve el camino caliente.
Esto es el otro: cuando una institución cambió de URL, cuando el fetch lleva dos
semanas fallando, o cuando hay que averiguar si una SOFIPO nueva publica tasas.
Es ocasional a propósito: cuesta más, tarda más y acierta menos.

## La invariante

> **Se acumulan las URLs que devolvieron búsquedas reales y todo hallazgo cuya
> URL no esté en ese conjunto se descarta.**

Es lo único que separa esto de un generador de fuentes plausibles. Un modelo
que no encuentra la página de tasas de Klar puede perfectamente contestar
`https://www.klar.mx/tasas` — una URL bien formada, del dominio correcto, que
no existe. Sin la invariante, esa cifra acabaría en la cola de revisión con
aspecto de estar respaldada; con ella, se cae antes de llegar.

Nótese que la invariante **no** exige que la URL responda 200 ni que diga lo
que el modelo afirma: eso lo verifica una persona. Lo que garantiza es más
modesto y más importante: que alguien vio esa URL en un buscador antes de que
el modelo la escribiera.

## El loop

Rondas con la herramienta `web_search` disponible. Tras `research_max_rondas`
se **retiran las herramientas** y se pide el JSON final: sin eso, un modelo que
no converge sigue buscando hasta agotar el presupuesto del día.

Y como el resto del nivel 2: **este módulo no decide**. Lo que encuentre pasa
por el mismo `reviewer`, con `fuente=LLM_RESEARCH`, y la primera lectura de un
producto siempre la aprueba una persona.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.config_store import effective
from core.logging import get_logger
from core.settings import settings
from domain.enums import TipoProducto
from llm.client import ClienteLLM
from rates_agent import prompts
from rates_agent.extractor import TASA_MAXIMA_PLAUSIBLE
from rates_agent.search import Resultado, SearchExecutor

log = get_logger(__name__)

#: El esquema de la única herramienta. En el formato de OpenAI, que es el que
#: habla el proveedor.
HERRAMIENTA_BUSQUEDA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Busca en la web. Devuelve título, URL y resumen de cada resultado. "
            "Las URLs que devuelve son las únicas que puedes citar."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "consulta": {
                    "type": "string",
                    "description": "Qué buscar, en español y con términos concretos.",
                }
            },
            "required": ["consulta"],
        },
    },
}


class Hallazgo(BaseModel):
    """Una tasa que el modelo dice haber encontrado, con su fuente."""

    model_config = ConfigDict(str_strip_whitespace=True)

    producto: str = Field(min_length=1, max_length=200)
    tipo: TipoProducto
    plazo_dias: int | None = None
    tasa_nominal: Decimal
    gat_nominal: Decimal | None = None
    gat_real: Decimal | None = None
    url: str
    confianza: Literal["alta", "media", "baja"] = "media"
    notas: str | None = None


@dataclass(slots=True)
class ReporteResearch:
    """Qué salió de investigar una institución."""

    institucion: str
    hallazgos: list[Hallazgo] = field(default_factory=list)
    rondas: int = 0
    busquedas: int = 0
    urls_vistas: int = 0
    descartados_por_url: list[str] = field(default_factory=list)
    tokens: int = 0
    costo_usd: float = 0.0
    sin_datos: bool = False
    notas: str | None = None

    def como_metricas(self) -> dict[str, Any]:
        return {
            "institucion": self.institucion,
            "hallazgos": len(self.hallazgos),
            "rondas": self.rondas,
            "busquedas": self.busquedas,
            "urls_vistas": self.urls_vistas,
            "descartados_por_url": self.descartados_por_url[:10],
            "tokens": self.tokens,
            "costo_usd": round(self.costo_usd, 6),
            "sin_datos": self.sin_datos,
        }


def normalizar_url(url: str) -> str:
    """Forma canónica para comparar contra `urls_permitidas`.

    Se ignoran el fragmento, la barra final y las mayúsculas del host. No es
    laxitud: un buscador devuelve `https://finsus.mx/inversiones` y el modelo
    la reescribe `https://finsus.mx/inversiones/` — la misma página. Lo que no
    se tolera es cambiar de host o de ruta, que es donde vive la invención.
    """
    partes = urlsplit(url.strip())
    ruta = partes.path.rstrip("/") or "/"
    return urlunsplit((partes.scheme.lower(), partes.netloc.lower(), ruta, partes.query, ""))


async def investigar(
    cliente: ClienteLLM,
    *,
    institucion: str,
    categoria: str,
    sitio: str | None,
    productos: list[str],
    contexto: str = "sin lecturas previas",
    ejecutor: SearchExecutor | None = None,
    max_rondas: int | None = None,
    hoy: date | None = None,
) -> ReporteResearch:
    """Busca la tasa vigente de una institución. Nunca publica nada."""
    ejecutor = ejecutor or SearchExecutor()
    # Del ConfigStore y no de Settings: es una de las llaves que la
    # calibración mueve en caliente.
    rondas_max = max_rondas if max_rondas is not None else int(effective.research_max_rondas)
    reporte = ReporteResearch(institucion=institucion)

    conversacion: list[dict[str, Any]] = [
        {"role": "system", "content": prompts.plantilla("research_system")},
        {
            "role": "user",
            "content": prompts.render(
                "research_user",
                institucion=institucion,
                categoria=categoria,
                sitio=sitio or "desconocido",
                fecha=(hoy or date.today()).isoformat(),
                productos="\n".join(f"- {p}" for p in productos) or "- (ninguno declarado)",
                contexto=contexto,
            ),
        },
    ]

    for ronda in range(rondas_max + 1):
        # En la última vuelta se retiran las herramientas: es lo que obliga al
        # modelo a entregar en vez de seguir buscando.
        ultima = ronda == rondas_max
        respuesta = await cliente.completar(
            sistema="",
            usuario="",
            mensajes=conversacion,
            herramientas=None if ultima else [HERRAMIENTA_BUSQUEDA],
        )
        reporte.rondas = ronda + 1
        reporte.tokens += respuesta.tokens_totales
        reporte.costo_usd += respuesta.costo_usd

        if not respuesta.herramientas or ultima:
            _leer_final(reporte, respuesta.contenido, ejecutor)
            break

        conversacion.append(
            {
                "role": "assistant",
                "content": respuesta.contenido or None,
                "tool_calls": [
                    {
                        "id": llamada.id,
                        "type": "function",
                        "function": {
                            "name": llamada.nombre,
                            "arguments": llamada.argumentos_crudos or "{}",
                        },
                    }
                    for llamada in respuesta.herramientas
                ],
            }
        )
        for llamada in respuesta.herramientas:
            conversacion.append(
                {
                    "role": "tool",
                    "tool_call_id": llamada.id,
                    "content": await _ejecutar(llamada.nombre, llamada.argumentos, ejecutor),
                }
            )
        reporte.busquedas = len(ejecutor.consultas)

    reporte.busquedas = len(ejecutor.consultas)
    reporte.urls_vistas = len(ejecutor.urls_permitidas)
    log.info("research_terminado", **reporte.como_metricas())
    return reporte


async def _ejecutar(nombre: str, argumentos: dict[str, Any], ejecutor: SearchExecutor) -> str:
    """Corre la herramienta y devuelve su resultado como texto para el modelo."""
    if nombre != "web_search":
        return json.dumps({"error": f"herramienta desconocida: {nombre}"}, ensure_ascii=False)

    consulta = str(argumentos.get("consulta") or argumentos.get("query") or "").strip()
    if not consulta:
        # Ocurre cuando el modelo manda argumentos rotos. Se le dice, en vez de
        # abortar: le queda una ronda para corregir.
        return json.dumps({"error": "falta el argumento 'consulta'"}, ensure_ascii=False)

    resultados: list[Resultado] = await ejecutor.buscar(
        consulta, maximo=settings.research_resultados_por_busqueda
    )
    return json.dumps(
        {"consulta": consulta, "resultados": [r.como_dict() for r in resultados]},
        ensure_ascii=False,
    )


def _leer_final(reporte: ReporteResearch, contenido: str, ejecutor: SearchExecutor) -> None:
    """Valida el JSON final y **aplica la invariante**."""
    from llm import parsers

    try:
        datos = parsers.parsear_json(contenido, claves_requeridas=("hallazgos",))
    except Exception as exc:  # noqa: BLE001 — el parser lanza su propio tipo
        log.warning("research_json_invalido", error=str(exc)[:200])
        reporte.sin_datos = True
        reporte.notas = f"el modelo no devolvió un JSON usable: {str(exc)[:160]}"
        return

    reporte.notas = str(datos.get("notas") or "") or None
    permitidas = {normalizar_url(u) for u in ejecutor.urls_permitidas}
    crudos = datos.get("hallazgos")
    if not isinstance(crudos, list) or not crudos:
        reporte.sin_datos = True
        return

    for crudo in crudos:
        try:
            hallazgo = Hallazgo.model_validate(crudo)
        except ValidationError as exc:
            log.warning("research_hallazgo_invalido", error=str(exc)[:200])
            continue

        if not (Decimal("0") <= hallazgo.tasa_nominal <= TASA_MAXIMA_PLAUSIBLE):
            log.warning("research_tasa_implausible", tasa=str(hallazgo.tasa_nominal))
            continue

        if normalizar_url(hallazgo.url) not in permitidas:
            # **La invariante.** El modelo citó una URL que ninguna búsqueda
            # devolvió: o se la inventó, o la reconstruyó de memoria. Las dos
            # cosas son lo mismo desde aquí.
            reporte.descartados_por_url.append(hallazgo.url)
            log.warning(
                "research_url_no_permitida",
                institucion=reporte.institucion,
                url=hallazgo.url[:200],
            )
            continue

        reporte.hallazgos.append(hallazgo)

    reporte.sin_datos = not reporte.hallazgos


__all__ = [
    "HERRAMIENTA_BUSQUEDA",
    "Hallazgo",
    "ReporteResearch",
    "investigar",
    "normalizar_url",
]
