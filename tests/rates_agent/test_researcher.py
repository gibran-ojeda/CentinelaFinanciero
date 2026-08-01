"""Tests del researcher, con la invariante `allowed_urls` en el centro.

Es el test que más importa de la fase 9: sin él, el nivel 3 es un generador de
fuentes plausibles. Un modelo que no encuentra la página de tasas de Klar
contesta `https://www.klar.mx/tasas` — bien formada, del dominio correcto, y
sin existir. Lo que se prueba aquí es que esa cifra no llega a ninguna parte.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest

from llm.providers.base import LlamadaHerramienta, RespuestaLLM
from rates_agent.researcher import investigar, normalizar_url
from rates_agent.search import Resultado, SearchExecutor


class MotorFalso:
    nombre = "falso"

    def __init__(self, urls: list[str]) -> None:
        self._urls = urls

    async def buscar(self, consulta: str, *, maximo: int) -> list[Resultado]:
        return [Resultado(titulo="t", url=u, resumen="r", motor=self.nombre) for u in self._urls]


class ClienteFalso:
    """Devuelve las respuestas que se le den, en orden."""

    def __init__(self, *respuestas: RespuestaLLM) -> None:
        self._respuestas = list(respuestas)
        self.llamadas: list[dict[str, Any]] = []

    async def completar(self, **kwargs: Any) -> RespuestaLLM:
        self.llamadas.append(kwargs)
        return self._respuestas.pop(0)


def _respuesta(
    contenido: str = "", *, tools: list[LlamadaHerramienta] | None = None
) -> RespuestaLLM:
    return RespuestaLLM(
        contenido=contenido,
        modelo="deepseek-v4-flash",
        tokens_entrada=100,
        tokens_salida=50,
        costo_usd=0.0001,
        latencia_ms=10,
        herramientas=tuple(tools or ()),
    )


def _busqueda(consulta: str = "tasas Klar") -> LlamadaHerramienta:
    return LlamadaHerramienta(
        id="call_1",
        nombre="web_search",
        argumentos={"consulta": consulta},
        argumentos_crudos=json.dumps({"consulta": consulta}),
    )


def _final(url: str, *, tasa: str = "12.5") -> str:
    return json.dumps(
        {
            "hallazgos": [
                {
                    "producto": "Inversión a plazo 90 días",
                    "tipo": "PLAZO",
                    "plazo_dias": 90,
                    "tasa_nominal": tasa,
                    "url": url,
                    "confianza": "alta",
                }
            ],
            "sin_datos": False,
        }
    )


async def _investigar(cliente: ClienteFalso, ejecutor: SearchExecutor, **extra: Any) -> Any:
    return await investigar(
        cliente,  # type: ignore[arg-type]
        institucion="Klar",
        categoria="SOFIPO",
        sitio="https://www.klar.mx/",
        productos=["Inversión a plazo 90 días"],
        ejecutor=ejecutor,
        **extra,
    )


# ─── La invariante ────────────────────────────────────────────


async def test_a_finding_backed_by_a_real_search_result_survives() -> None:
    ejecutor = SearchExecutor([MotorFalso(["https://www.klar.mx/inversion"])])  # type: ignore[list-item]
    cliente = ClienteFalso(
        _respuesta(tools=[_busqueda()]),
        _respuesta(_final("https://www.klar.mx/inversion")),
    )

    reporte = await _investigar(cliente, ejecutor)

    assert len(reporte.hallazgos) == 1
    assert reporte.hallazgos[0].tasa_nominal == Decimal("12.5")
    assert reporte.descartados_por_url == []


async def test_an_invented_url_is_discarded() -> None:
    """La URL es plausible, del dominio correcto, y ninguna búsqueda la vio."""
    ejecutor = SearchExecutor([MotorFalso(["https://www.klar.mx/inversion"])])  # type: ignore[list-item]
    cliente = ClienteFalso(
        _respuesta(tools=[_busqueda()]),
        _respuesta(_final("https://www.klar.mx/tasas")),
    )

    reporte = await _investigar(cliente, ejecutor)

    assert reporte.hallazgos == []
    assert reporte.descartados_por_url == ["https://www.klar.mx/tasas"]
    assert reporte.sin_datos is True


async def test_without_any_search_nothing_can_be_cited() -> None:
    """Si el modelo contesta a la primera, no hay URL permitida que valga."""
    ejecutor = SearchExecutor([MotorFalso(["https://www.klar.mx/inversion"])])  # type: ignore[list-item]
    cliente = ClienteFalso(_respuesta(_final("https://www.klar.mx/inversion")))

    reporte = await _investigar(cliente, ejecutor)

    assert reporte.hallazgos == []
    assert reporte.descartados_por_url == ["https://www.klar.mx/inversion"]


async def test_a_trailing_slash_is_not_an_invention() -> None:
    """El buscador devuelve una y el modelo la reescribe con barra. Es la misma."""
    ejecutor = SearchExecutor([MotorFalso(["https://www.klar.mx/inversion"])])  # type: ignore[list-item]
    cliente = ClienteFalso(
        _respuesta(tools=[_busqueda()]),
        _respuesta(_final("https://WWW.KLAR.MX/inversion/")),
    )

    reporte = await _investigar(cliente, ejecutor)

    assert len(reporte.hallazgos) == 1


def test_normalising_keeps_host_and_path_significant() -> None:
    """Lo que se tolera es cosmético. Cambiar de host o de ruta no lo es."""
    base = normalizar_url("https://klar.mx/a")

    assert normalizar_url("https://klar.mx/a/") == base
    assert normalizar_url("https://KLAR.mx/a#seccion") == base
    assert normalizar_url("https://klar.mx/b") != base
    assert normalizar_url("https://otro.mx/a") != base


# ─── El loop ──────────────────────────────────────────────────


async def test_tools_are_withdrawn_on_the_last_round() -> None:
    """Sin esto, un modelo que no converge gasta el presupuesto del día."""
    ejecutor = SearchExecutor([MotorFalso(["https://a.test/1"])])  # type: ignore[list-item]
    cliente = ClienteFalso(
        _respuesta(tools=[_busqueda()]),
        _respuesta(tools=[_busqueda("otra")]),
        _respuesta(_final("https://a.test/1")),
    )

    reporte = await _investigar(cliente, ejecutor, max_rondas=2)

    assert reporte.rondas == 3
    assert cliente.llamadas[-1]["herramientas"] is None
    assert cliente.llamadas[0]["herramientas"] is not None


async def test_the_search_results_go_back_to_the_model() -> None:
    ejecutor = SearchExecutor([MotorFalso(["https://a.test/1"])])  # type: ignore[list-item]
    cliente = ClienteFalso(
        _respuesta(tools=[_busqueda()]),
        _respuesta(_final("https://a.test/1")),
    )

    await _investigar(cliente, ejecutor)

    conversacion = cliente.llamadas[-1]["mensajes"]
    tool = next(m for m in conversacion if m["role"] == "tool")
    assert "https://a.test/1" in tool["content"]


async def test_broken_tool_arguments_get_an_error_back_not_a_crash() -> None:
    """Un modelo económico manda argumentos rotos. Le queda una ronda."""
    ejecutor = SearchExecutor([MotorFalso(["https://a.test/1"])])  # type: ignore[list-item]
    rota = LlamadaHerramienta(id="c1", nombre="web_search", argumentos={}, argumentos_crudos="{")
    cliente = ClienteFalso(
        _respuesta(tools=[rota]),
        _respuesta(json.dumps({"hallazgos": [], "sin_datos": True})),
    )

    reporte = await _investigar(cliente, ejecutor)

    conversacion = cliente.llamadas[-1]["mensajes"]
    tool = next(m for m in conversacion if m["role"] == "tool")
    assert "error" in tool["content"]
    assert reporte.sin_datos is True


async def test_an_unknown_tool_is_answered_not_executed() -> None:
    ejecutor = SearchExecutor([MotorFalso(["https://a.test/1"])])  # type: ignore[list-item]
    inventada = LlamadaHerramienta(id="c1", nombre="leer_pagina", argumentos={})
    cliente = ClienteFalso(
        _respuesta(tools=[inventada]),
        _respuesta(json.dumps({"hallazgos": [], "sin_datos": True})),
    )

    await _investigar(cliente, ejecutor)

    tool = next(m for m in cliente.llamadas[-1]["mensajes"] if m["role"] == "tool")
    assert "desconocida" in tool["content"]
    assert ejecutor.consultas == []


# ─── Respuestas finales que no sirven ─────────────────────────


async def test_no_data_is_a_valid_answer() -> None:
    """Se pide explícitamente en el prompt: es frecuente y no es un fallo."""
    ejecutor = SearchExecutor([MotorFalso([])])  # type: ignore[list-item]
    cliente = ClienteFalso(
        _respuesta(tools=[_busqueda()]),
        _respuesta(json.dumps({"hallazgos": [], "sin_datos": True, "notas": "nada vigente"})),
    )

    reporte = await _investigar(cliente, ejecutor)

    assert reporte.sin_datos is True
    assert reporte.notas == "nada vigente"


async def test_a_final_answer_that_is_not_json_does_not_raise() -> None:
    ejecutor = SearchExecutor([MotorFalso(["https://a.test/1"])])  # type: ignore[list-item]
    cliente = ClienteFalso(
        _respuesta(tools=[_busqueda()]),
        _respuesta("no encontré nada, lo siento"),
    )

    reporte = await _investigar(cliente, ejecutor)

    assert reporte.sin_datos is True
    assert reporte.hallazgos == []


async def test_an_implausible_rate_is_dropped() -> None:
    ejecutor = SearchExecutor([MotorFalso(["https://a.test/1"])])  # type: ignore[list-item]
    cliente = ClienteFalso(
        _respuesta(tools=[_busqueda()]),
        _respuesta(_final("https://a.test/1", tasa="950")),
    )

    reporte = await _investigar(cliente, ejecutor)

    assert reporte.hallazgos == []


async def test_a_finding_that_does_not_validate_is_dropped_not_fatal() -> None:
    """Un producto a la vista con plazo es un modelo equivocado, no una tasa."""
    ejecutor = SearchExecutor([MotorFalso(["https://a.test/1"])])  # type: ignore[list-item]
    malo = json.dumps(
        {
            "hallazgos": [
                {
                    "producto": "x",
                    "tipo": "INVENTADO",
                    "tasa_nominal": 5,
                    "url": "https://a.test/1",
                },
                {
                    "producto": "Plazo 90",
                    "tipo": "PLAZO",
                    "plazo_dias": 90,
                    "tasa_nominal": 10,
                    "url": "https://a.test/1",
                },
            ]
        }
    )
    cliente = ClienteFalso(_respuesta(tools=[_busqueda()]), _respuesta(malo))

    reporte = await _investigar(cliente, ejecutor)

    assert len(reporte.hallazgos) == 1
    assert reporte.hallazgos[0].producto == "Plazo 90"


async def test_the_run_reports_what_it_cost() -> None:
    ejecutor = SearchExecutor([MotorFalso(["https://a.test/1"])])  # type: ignore[list-item]
    cliente = ClienteFalso(
        _respuesta(tools=[_busqueda()]),
        _respuesta(_final("https://a.test/1")),
    )

    reporte = await _investigar(cliente, ejecutor)

    assert reporte.tokens == 300
    assert reporte.costo_usd == pytest.approx(0.0002)
    assert reporte.busquedas == 1
    assert reporte.urls_vistas == 1
