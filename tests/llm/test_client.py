"""Tests del cliente: presupuesto, reintentos y parseo, juntos.

Lo que se verifica aquí es la política, no el transporte: qué se reintenta, qué
no, y qué se hace antes y después de gastar. El proveedor es un doble porque el
transporte ya está probado en `test_provider.py` contra la API real.
"""

from __future__ import annotations

from typing import Any, Literal

import pytest

from llm.client import ClienteLLM
from llm.providers.base import (
    ErrorDeParseo,
    ErrorLimiteDePeticiones,
    ErrorPresupuestoAgotado,
    ErrorProveedor,
    ErrorTiempoAgotado,
    ProveedorLLM,
    RespuestaLLM,
)

pytestmark = pytest.mark.requires_docker


def _respuesta(contenido: str = '{"ok": true}', costo: float = 0.001) -> RespuestaLLM:
    return RespuestaLLM(
        contenido=contenido,
        modelo="doble",
        tokens_entrada=100,
        tokens_salida=20,
        costo_usd=costo,
        latencia_ms=5,
    )


class ProveedorDoble(ProveedorLLM):
    """Devuelve lo que se le ponga en `guion`, en orden."""

    def __init__(self, *guion: RespuestaLLM | Exception) -> None:
        self.nombre = "doble"
        self.modelo = "doble"
        self._guion = list(guion)
        self.llamadas = 0

    async def completar(
        self,
        *,
        sistema: str,
        usuario: str,
        temperatura: float = 0.0,
        max_tokens: int = 4000,
        formato: Literal["texto", "json"] = "json",
        # El tool-loop del researcher los usa; aquí sólo hay que aceptarlos.
        mensajes: list[dict[str, Any]] | None = None,
        herramientas: list[dict[str, Any]] | None = None,
    ) -> RespuestaLLM:
        self.llamadas += 1
        self.ultimas_herramientas = herramientas
        siguiente = self._guion.pop(0) if self._guion else _respuesta()
        if isinstance(siguiente, Exception):
            raise siguiente
        return siguiente

    async def ping(self) -> bool:
        return True


@pytest.mark.usefixtures("real_redis")
class TestPolitica:
    async def test_a_transient_error_is_retried(self) -> None:
        from llm import cost_tracker

        await cost_tracker.reiniciar()
        doble = ProveedorDoble(ErrorLimiteDePeticiones("429", retry_after=0.01), _respuesta())
        cliente = ClienteLLM(doble, espera_base_s=0.01, espera_tope_s=0.02)

        r = await cliente.completar(sistema="s", usuario="u")

        assert doble.llamadas == 2
        assert r.contenido == '{"ok": true}'

    async def test_a_timeout_is_retried_too(self) -> None:
        from llm import cost_tracker

        await cost_tracker.reiniciar()
        doble = ProveedorDoble(ErrorTiempoAgotado("agotado"), _respuesta())
        cliente = ClienteLLM(doble, espera_base_s=0.01)

        await cliente.completar(sistema="s", usuario="u")

        assert doble.llamadas == 2

    async def test_a_permanent_error_is_not_retried(self) -> None:
        """Un 401 no mejora esperando: reintentarlo sólo alarga el fallo."""
        from llm import cost_tracker

        await cost_tracker.reiniciar()
        doble = ProveedorDoble(ErrorProveedor("401 invalid api key"))
        cliente = ClienteLLM(doble, espera_base_s=0.01)

        with pytest.raises(ErrorProveedor):
            await cliente.completar(sistema="s", usuario="u")

        assert doble.llamadas == 1

    async def test_retries_run_out_and_the_last_error_propagates(self) -> None:
        from llm import cost_tracker

        await cost_tracker.reiniciar()
        doble = ProveedorDoble(*[ErrorTiempoAgotado("agotado")] * 5)
        cliente = ClienteLLM(doble, max_reintentos=2, espera_base_s=0.01)

        with pytest.raises(ErrorTiempoAgotado):
            await cliente.completar(sistema="s", usuario="u")

        assert doble.llamadas == 3  # el inicial + dos reintentos

    async def test_the_ceiling_stops_the_call_before_spending(self) -> None:
        """Se pregunta antes de gastar: con el techo alcanzado no hay petición."""
        from llm import cost_tracker

        await cost_tracker.reiniciar()
        await cost_tracker.registrar(1.0)
        doble = ProveedorDoble()
        cliente = ClienteLLM(doble, limite_diario_usd=1.0)

        with pytest.raises(ErrorPresupuestoAgotado):
            await cliente.completar(sistema="s", usuario="u")

        assert doble.llamadas == 0

    async def test_the_ceiling_is_read_hot_from_config(self) -> None:
        """Sin techo explícito manda la llave del ConfigStore, en cada llamada.

        Es lo que permite bajar el techo a media calibración sin deploy y sin
        reconstruir el cliente que ya vive dentro de la corrida.
        """
        import time

        import core.config_store as cs
        from llm import cost_tracker

        await cost_tracker.reiniciar()
        doble = ProveedorDoble()
        cliente = ClienteLLM(doble)

        previo = cs._snapshot
        cs._snapshot = cs.ConfigSnapshot(
            values={"llm_cost_daily_limit_usd": 0.0}, loaded_at=time.monotonic()
        )
        try:
            with pytest.raises(ErrorPresupuestoAgotado):
                await cliente.completar(sistema="s", usuario="u")
        finally:
            cs._snapshot = previo

        assert doble.llamadas == 0

    async def test_the_cost_is_recorded_after_a_successful_call(self) -> None:
        from llm import cost_tracker

        await cost_tracker.reiniciar()
        cliente = ClienteLLM(ProveedorDoble(_respuesta(costo=0.0025)))

        await cliente.completar(sistema="s", usuario="u")

        assert await cost_tracker.gastado_hoy() == pytest.approx(0.0025)

    async def test_a_failed_call_costs_nothing(self) -> None:
        from llm import cost_tracker

        await cost_tracker.reiniciar()
        cliente = ClienteLLM(ProveedorDoble(ErrorProveedor("401")), espera_base_s=0.01)

        with pytest.raises(ErrorProveedor):
            await cliente.completar(sistema="s", usuario="u")

        assert await cost_tracker.gastado_hoy() == 0.0

    async def test_json_helper_parses_and_validates_keys(self) -> None:
        from llm import cost_tracker

        await cost_tracker.reiniciar()
        cliente = ClienteLLM(ProveedorDoble(_respuesta('```json\n{"tasa": 8.69}\n```')))

        datos, respuesta = await cliente.completar_json(
            sistema="s", usuario="u", claves_requeridas=("tasa",)
        )

        assert datos == {"tasa": 8.69}
        assert respuesta.costo_usd == 0.001

    async def test_json_helper_raises_when_a_required_key_is_missing(self) -> None:
        from llm import cost_tracker

        await cost_tracker.reiniciar()
        cliente = ClienteLLM(ProveedorDoble(_respuesta('{"tasa": 8.69}')))

        with pytest.raises(ErrorDeParseo, match="faltan claves"):
            await cliente.completar_json(
                sistema="s", usuario="u", claves_requeridas=("tasa", "plazo_dias")
            )
