"""Tests del extractor.

Cada caso viene de algo que estas páginas hacen de verdad: «hasta 15 %» sin
plazo, plazos que no son los de CETES, GAT que no cuadra con la nominal, y
páginas que sólo traen publicidad.

El contrato se prueba en dos planos: la validación del modelo pydantic —que es
lo que impide que un error del LLM se convierta en un dato publicado— y el
comportamiento del reintento.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from domain.enums import TipoProducto
from llm.client import ClienteLLM
from llm.providers.base import ErrorDeParseo, ProveedorLLM, RespuestaLLM
from rates_agent.extractor import Extraccion, TasaExtraida, extraer

pytestmark = pytest.mark.requires_docker


# ─── La validación del contrato ───────────────────────────────


def test_a_term_product_needs_its_tenor() -> None:
    with pytest.raises(ValueError, match="necesita plazo_dias"):
        TasaExtraida(producto="Plazo fijo", tipo=TipoProducto.PLAZO, tasa_nominal=Decimal("8.69"))


def test_a_sight_product_must_not_carry_a_tenor() -> None:
    """`plazo_dias: 0` para una cuenta a la vista es un modelo equivocándose."""
    with pytest.raises(ValueError, match="no lleva plazo_dias"):
        TasaExtraida(
            producto="Cuenta",
            tipo=TipoProducto.VISTA,
            plazo_dias=30,
            tasa_nominal=Decimal("8.50"),
        )


def test_an_implausible_rate_is_rejected() -> None:
    """950 en vez de 9.50 es el error de captura clásico."""
    with pytest.raises(ValueError, match="fuera de rango plausible"):
        TasaExtraida(
            producto="Plazo",
            tipo=TipoProducto.PLAZO,
            plazo_dias=360,
            tasa_nominal=Decimal("950"),
        )


def test_a_negative_real_gat_is_valid() -> None:
    """Es lo que pasa cuando el rendimiento no alcanza a la inflación.

    Y es justo el número que este proyecto existe para enseñar, así que
    rechazarlo sería rechazar el caso interesante.
    """
    t = TasaExtraida(
        producto="Cuenta",
        tipo=TipoProducto.VISTA,
        tasa_nominal=Decimal("3.00"),
        gat_real=Decimal("-0.59"),
    )
    assert t.gat_real == Decimal("-0.59")


def test_an_inconsistent_gat_is_kept_as_published() -> None:
    """No se recalcula ni se corrige: esa inconsistencia es la señal."""
    t = TasaExtraida(
        producto="Cuenta",
        tipo=TipoProducto.VISTA,
        tasa_nominal=Decimal("8.50"),
        gat_nominal=Decimal("3.04"),
    )
    assert t.tasa_nominal == Decimal("8.50")
    assert t.gat_nominal == Decimal("3.04")


def test_the_institution_tenor_is_kept_verbatim() -> None:
    """360 es 360. No se redondea a 364 porque CETES tenga esa serie."""
    t = TasaExtraida(
        producto="Plazo",
        tipo=TipoProducto.PLAZO,
        plazo_dias=360,
        tasa_nominal=Decimal("8.69"),
    )
    assert t.plazo_dias == 360


# ─── El extractor contra un modelo doble ──────────────────────


class ProveedorGuionado(ProveedorLLM):
    def __init__(self, *respuestas: str) -> None:
        self.nombre = "doble"
        self.modelo = "doble"
        self._guion = list(respuestas)
        self.llamadas = 0
        self.ultimo_usuario = ""

    async def completar(self, **kwargs: object) -> RespuestaLLM:
        self.llamadas += 1
        self.ultimo_usuario = str(kwargs.get("usuario", ""))
        contenido = self._guion.pop(0) if self._guion else '{"tasas": []}'
        return RespuestaLLM(
            contenido=contenido,
            modelo="doble",
            tokens_entrada=100,
            tokens_salida=50,
            costo_usd=0.0001,
            latencia_ms=1,
        )

    async def ping(self) -> bool:
        return True


def _cliente(*respuestas: str) -> tuple[ClienteLLM, ProveedorGuionado]:
    doble = ProveedorGuionado(*respuestas)
    return ClienteLLM(doble), doble


@pytest.mark.usefixtures("real_redis")
class TestExtraer:
    async def test_a_real_table_becomes_structured_rates(self) -> None:
        cuerpo = json.dumps(
            {
                "tasas": [
                    {
                        "producto": "Inversión Plazo Fijo",
                        "tipo": "PLAZO",
                        "plazo_dias": 360,
                        "tasa_nominal": "8.69",
                        "gat_nominal": "8.69",
                        "gat_real": "4.56",
                        "monto_minimo": "100.00",
                        "condiciones": "Tasa fija, antes de impuestos.",
                        "confianza": "alta",
                    }
                ]
            }
        )
        cliente, _ = _cliente(cuerpo)

        resultado = await extraer(
            cliente, institucion="Finsus", url="https://finsus.test/", contenido="…"
        )

        assert len(resultado.tasas) == 1
        assert resultado.tasas[0].plazo_dias == 360
        assert resultado.tasas[0].tasa_nominal == Decimal("8.69")

    async def test_a_page_with_no_rates_is_a_valid_answer(self) -> None:
        """Muchas del catálogo sólo traen publicidad. Vacío no es un fallo."""
        cliente, doble = _cliente('{"tasas": []}')

        resultado = await extraer(
            cliente, institucion="Klar", url="https://klar.test/", contenido="…"
        )

        assert resultado.tasas == []
        assert doble.llamadas == 1  # no se reintenta un vacío legítimo

    async def test_one_bad_entry_does_not_cost_the_good_ones(self) -> None:
        """Que el modelo confunda un plazo no debe tirar las otras tasas."""
        cuerpo = json.dumps(
            {
                "tasas": [
                    {
                        "producto": "Cuenta",
                        "tipo": "VISTA",
                        "plazo_dias": 30,  # incoherente: se descarta
                        "tasa_nominal": "8.50",
                    },
                    {
                        "producto": "Plazo 180",
                        "tipo": "PLAZO",
                        "plazo_dias": 180,
                        "tasa_nominal": "7.59",
                    },
                ]
            }
        )
        cliente, doble = _cliente(cuerpo)

        resultado = await extraer(cliente, institucion="X", url="https://x.test/", contenido="…")

        assert len(resultado.tasas) == 1
        assert resultado.tasas[0].plazo_dias == 180
        assert doble.llamadas == 1

    async def test_an_all_invalid_response_is_retried_with_the_error(self) -> None:
        malo = json.dumps({"tasas": [{"producto": "P", "tipo": "PLAZO", "tasa_nominal": "8.0"}]})
        bueno = json.dumps(
            {
                "tasas": [
                    {
                        "producto": "P",
                        "tipo": "PLAZO",
                        "plazo_dias": 360,
                        "tasa_nominal": "8.0",
                    }
                ]
            }
        )
        cliente, doble = _cliente(malo, bueno)

        resultado = await extraer(cliente, institucion="X", url="https://x.test/", contenido="…")

        assert doble.llamadas == 2
        assert "no fue válida" in doble.ultimo_usuario
        assert len(resultado.tasas) == 1

    async def test_two_invalid_answers_leave_the_page_unextracted(self) -> None:
        """Mejor sin extracción que con algo que no pasó el contrato."""
        malo = json.dumps({"tasas": [{"producto": "P", "tipo": "PLAZO", "tasa_nominal": "8.0"}]})
        cliente, doble = _cliente(malo, malo)

        with pytest.raises(ErrorDeParseo, match="no validó tras el reintento"):
            await extraer(cliente, institucion="X", url="https://x.test/", contenido="…")

        assert doble.llamadas == 2

    async def test_the_prompt_carries_the_institution_and_the_url(self) -> None:
        cliente, doble = _cliente('{"tasas": []}')

        await extraer(
            cliente,
            institucion="Supertasas",
            url="https://supertasas.test/inversion",
            contenido="tabla de tasas",
        )

        assert "Supertasas" in doble.ultimo_usuario
        assert "https://supertasas.test/inversion" in doble.ultimo_usuario
        assert "tabla de tasas" in doble.ultimo_usuario

    async def test_long_pages_are_cut_from_the_end(self) -> None:
        """La tabla suele estar arriba; el pie es aviso legal repetido."""
        cliente, doble = _cliente('{"tasas": []}')

        await extraer(
            cliente,
            institucion="X",
            url="https://x.test/",
            contenido="INICIO" + ("x" * 50_000) + "FINAL",
            max_caracteres=1000,
        )

        assert "INICIO" in doble.ultimo_usuario
        assert "FINAL" not in doble.ultimo_usuario


def test_the_extraction_defaults_to_no_rates() -> None:
    assert Extraccion().tasas == []


# ─── Las plantillas ───────────────────────────────────────────


def test_the_prompt_templates_load_and_render() -> None:
    from rates_agent import prompts

    sistema = prompts.plantilla("extract_rates_system")
    usuario = prompts.render(
        "extract_rates_user",
        institucion="Finsus",
        url="https://finsus.test/",
        fecha="2026-07-29",
        contenido="8.69% a 360 días",
    )

    # Las reglas duras están en el system prompt, no en el código.
    assert "Hasta X %" in sistema
    assert "360 días, es" in sistema
    assert "Sin tasas" in sistema
    # Y el ejemplo de JSON sobrevive al renderizado: sus llaves van dobladas.
    assert '{{"tasas": []}}' not in usuario
    assert '{"tasas": []}' in usuario
    assert "Finsus" in usuario
