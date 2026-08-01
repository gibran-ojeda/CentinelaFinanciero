"""Tests del proveedor contra la forma real de la respuesta de DeepSeek.

Se usa `respx` en vez de un doble: lo que hay que verificar es que se lee bien
un JSON con la estructura de OpenAI y que cada código de estado cae en el error
correcto — un doble reproduciría lo que ya creemos, no lo que la API manda.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from llm.providers.base import (
    ErrorDeParseo,
    ErrorLimiteDePeticiones,
    ErrorProveedor,
    ErrorTiempoAgotado,
)
from llm.providers.openai_compat import (
    PRECIO_DESCONOCIDO,
    PRECIOS,
    ProveedorOpenAICompat,
    costo_usd,
)

BASE = "https://api.deepseek.test/v1"
RUTA = f"{BASE}/chat/completions"


def _proveedor(modelo: str = "deepseek-v4-flash") -> ProveedorOpenAICompat:
    return ProveedorOpenAICompat(
        api_key="llave-de-prueba", modelo=modelo, base_url=BASE, timeout_s=5.0
    )


def _respuesta(contenido: str = '{"ok": true}', **extra: object) -> dict:
    mensaje = {"content": contenido, "role": "assistant"}
    mensaje.update(extra)
    return {
        "id": "chatcmpl-1",
        "model": "deepseek-v4-flash",
        "choices": [{"index": 0, "message": mensaje, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500},
    }


@respx.mock
async def test_a_normal_completion_is_normalized() -> None:
    respx.post(RUTA).mock(return_value=httpx.Response(200, json=_respuesta()))

    async with _proveedor() as p:
        r = await p.completar(sistema="s", usuario="u")

    assert r.contenido == '{"ok": true}'
    assert (r.tokens_entrada, r.tokens_salida, r.tokens_totales) == (1200, 300, 1500)
    assert r.finish_reason == "stop"
    assert r.costo_usd == pytest.approx((1200 * 0.14 + 300 * 0.28) / 1_000_000)


@respx.mock
async def test_json_format_is_requested_when_asked() -> None:
    ruta = respx.post(RUTA).mock(return_value=httpx.Response(200, json=_respuesta()))

    async with _proveedor() as p:
        await p.completar(sistema="s", usuario="u", formato="json")

    assert ruta.calls.last.request.read().decode().count('"json_object"') == 1


@respx.mock
async def test_the_reasoning_channel_travels_separately() -> None:
    """Separado del contenido: cuando el contenido llega vacío es el respaldo."""
    respx.post(RUTA).mock(
        return_value=httpx.Response(
            200, json=_respuesta(contenido="", reasoning_content='{"tasa": 8.69}')
        )
    )

    async with _proveedor() as p:
        r = await p.completar(sistema="s", usuario="u")

    assert r.contenido == ""
    assert r.razonamiento == '{"tasa": 8.69}'


@respx.mock
async def test_429_is_a_rate_limit_with_its_retry_after() -> None:
    respx.post(RUTA).mock(return_value=httpx.Response(429, headers={"retry-after": "12"}))

    async with _proveedor() as p:
        with pytest.raises(ErrorLimiteDePeticiones) as exc:
            await p.completar(sistema="s", usuario="u")

    assert exc.value.retry_after == 12.0


@respx.mock
async def test_401_is_not_retryable() -> None:
    """Un 401 no mejora esperando, así que no comparte tipo con el 429."""
    respx.post(RUTA).mock(return_value=httpx.Response(401, text="invalid api key"))

    async with _proveedor() as p:
        with pytest.raises(ErrorProveedor) as exc:
            await p.completar(sistema="s", usuario="u")

    assert not isinstance(exc.value, ErrorLimiteDePeticiones)
    assert "401" in str(exc.value)


@respx.mock
async def test_a_timeout_has_its_own_type() -> None:
    respx.post(RUTA).mock(side_effect=httpx.ConnectTimeout("agotado"))

    async with _proveedor() as p:
        with pytest.raises(ErrorTiempoAgotado):
            await p.completar(sistema="s", usuario="u")


@respx.mock
async def test_a_malformed_body_is_a_parse_error_with_the_raw_text() -> None:
    respx.post(RUTA).mock(return_value=httpx.Response(200, json={"sin": "choices"}))

    async with _proveedor() as p:
        with pytest.raises(ErrorDeParseo) as exc:
            await p.completar(sistema="s", usuario="u")

    assert "sin" in exc.value.contenido_crudo


async def test_a_missing_api_key_fails_before_the_request() -> None:
    p = ProveedorOpenAICompat(api_key="", modelo="deepseek-v4-flash", base_url=BASE)
    with pytest.raises(ErrorProveedor, match="API key"):
        await p.completar(sistema="s", usuario="u")


# ─── Tool use ─────────────────────────────────────────────────


def _con_tools(*llamadas: dict[str, object]) -> dict:
    return _respuesta(contenido="", tool_calls=list(llamadas))


def _tool(id_: str, nombre: str, argumentos: str) -> dict[str, object]:
    return {"id": id_, "type": "function", "function": {"name": nombre, "arguments": argumentos}}


@respx.mock
async def test_tool_calls_are_normalized() -> None:
    respx.post(RUTA).mock(
        return_value=httpx.Response(
            200,
            json=_con_tools(_tool("call_1", "web_search", '{"query": "tasas Finsus"}')),
        )
    )

    async with _proveedor() as p:
        r = await p.completar(
            sistema="s",
            usuario="u",
            herramientas=[{"type": "function", "function": {"name": "web_search"}}],
        )

    assert len(r.herramientas) == 1
    llamada = r.herramientas[0]
    assert (llamada.id, llamada.nombre) == ("call_1", "web_search")
    assert llamada.argumentos == {"query": "tasas Finsus"}


@respx.mock
async def test_tools_are_sent_and_json_format_is_not() -> None:
    """`response_format` y `tools` juntos no se llevan.

    Con el formato JSON exigido, el modelo no puede contestar con una llamada a
    herramienta — que no es un objeto del esquema pedido.
    """
    ruta = respx.post(RUTA).mock(return_value=httpx.Response(200, json=_con_tools()))

    async with _proveedor() as p:
        await p.completar(
            sistema="s",
            usuario="u",
            formato="json",
            herramientas=[{"type": "function", "function": {"name": "web_search"}}],
        )

    enviado = ruta.calls.last.request.read().decode()
    assert '"tools"' in enviado
    assert "json_object" not in enviado


@respx.mock
async def test_without_tools_the_json_format_comes_back() -> None:
    """Es la última ronda: se retiran las tools y se exige el JSON final."""
    ruta = respx.post(RUTA).mock(return_value=httpx.Response(200, json=_respuesta()))

    async with _proveedor() as p:
        await p.completar(sistema="s", usuario="u", formato="json")

    assert "json_object" in ruta.calls.last.request.read().decode()


@respx.mock
async def test_a_full_conversation_replaces_the_two_messages() -> None:
    ruta = respx.post(RUTA).mock(return_value=httpx.Response(200, json=_respuesta()))
    conversacion = [
        {"role": "system", "content": "reglas"},
        {"role": "user", "content": "busca"},
        {"role": "assistant", "content": None, "tool_calls": []},
        {"role": "tool", "tool_call_id": "call_1", "content": "resultados"},
    ]

    async with _proveedor() as p:
        await p.completar(sistema="ignorado", usuario="ignorado", mensajes=conversacion)

    enviado = ruta.calls.last.request.read().decode()
    assert "ignorado" not in enviado
    assert "tool_call_id" in enviado


@respx.mock
async def test_broken_tool_arguments_do_not_raise() -> None:
    """Un modelo económico manda JSON roto de vez en cuando.

    Abortar la corrida sale más caro que devolverle el error como resultado de
    la herramienta y dejar que lo intente otra vez.
    """
    respx.post(RUTA).mock(
        return_value=httpx.Response(
            200, json=_con_tools(_tool("call_1", "web_search", '{"query": rota'))
        )
    )

    async with _proveedor() as p:
        r = await p.completar(sistema="s", usuario="u")

    assert r.herramientas[0].argumentos == {}
    assert "rota" in r.herramientas[0].argumentos_crudos


@respx.mock
async def test_a_response_without_tools_has_an_empty_tuple() -> None:
    respx.post(RUTA).mock(return_value=httpx.Response(200, json=_respuesta()))

    async with _proveedor() as p:
        r = await p.completar(sistema="s", usuario="u")

    assert r.herramientas == ()


def test_an_unknown_model_is_not_free() -> None:
    """Precio cero haría invisible el gasto justo donde el techo es la red."""
    assert "modelo-inventado" not in PRECIOS
    assert costo_usd("modelo-inventado", 1_000_000, 0) == pytest.approx(PRECIO_DESCONOCIDO[0])


def test_the_retired_deepseek_models_keep_their_price() -> None:
    """Retirados el 2026-07-24; el precio queda para recalcular costo histórico."""
    assert PRECIOS["deepseek-chat"] == (0.14, 0.28)
