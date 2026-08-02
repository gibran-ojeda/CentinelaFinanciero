"""Tests de la calculadora.

Incluye los ejemplos numéricos del foundation servidos por la API, no sólo por
el motor: verifican que la cadena completa —datos, contexto y cálculo— produce
los números que el documento promete.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from core.db import session_scope
from domain.orm import Producto

pytestmark = [pytest.mark.requires_docker, pytest.mark.usefixtures("comparador_poblado")]

RUTA = "/api/v1/calculadora"


async def _producto_id(slug: str) -> int:
    async with session_scope() as session:
        producto_id = await session.scalar(select(Producto.id).where(Producto.slug == slug))
    assert producto_id is not None
    return producto_id


async def _calcular(api: AsyncClient, **payload: object) -> dict:
    respuesta = await api.post(RUTA, json=payload)
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


# ─── Básico ───────────────────────────────────────────────────


async def test_requires_authentication(api: AsyncClient) -> None:
    respuesta = await api.post(RUTA, json={"monto": "1000", "producto_ids": [1]})
    assert respuesta.status_code == 401


async def test_returns_the_five_cascade_concepts(api_lectura: AsyncClient) -> None:
    cuerpo = await _calcular(
        api_lectura, monto="100000", producto_ids=[await _producto_id("cetes-28")]
    )

    cascada = cuerpo["resultados"][0]["cascada"]
    assert set(cascada) >= {
        "rendimiento_bruto",
        "isr_retenido",
        "rendimiento_neto",
        "efecto_inflacion",
        "ganancia_real",
    }


async def test_the_cascade_adds_up_exactly(api_lectura: AsyncClient) -> None:
    """El usuario va a sumar los números que ve."""
    cascada = (
        await _calcular(
            api_lectura, monto="100000", producto_ids=[await _producto_id("cetes-364")]
        )
    )["resultados"][0]["cascada"]

    bruto = Decimal(cascada["rendimiento_bruto"])
    partes = (
        Decimal(cascada["isr_retenido"])
        + Decimal(cascada["efecto_inflacion"])
        + Decimal(cascada["ganancia_real"])
    )
    assert bruto == partes


async def test_every_result_carries_the_fiscal_note(api_lectura: AsyncClient) -> None:
    """§6: qué retención se aplicó y cuándo se actualizó."""
    cuerpo = await _calcular(
        api_lectura, monto="10000", producto_ids=[await _producto_id("cetes-28")]
    )

    nota = cuerpo["resultados"][0]["cascada"]["nota_fiscal"]
    assert "0.90" in nota
    assert "2026-01-01" in nota
    assert "acreditable" in nota


async def test_the_response_carries_the_disclaimer(api_lectura: AsyncClient) -> None:
    """§19: la plataforma no es asesor financiero."""
    cuerpo = await _calcular(
        api_lectura, monto="10000", producto_ids=[await _producto_id("cetes-28")]
    )
    assert "no es asesor financiero" in cuerpo["disclaimer"]


# ─── Números del foundation, servidos por la API ──────────────


async def test_current_cetes_yield_the_expected_real_gain(
    api_lectura: AsyncClient,
) -> None:
    """Con los datos reales del seed y un año completo.

    CETES 28d al 6.18% (subasta del 23 de julio), retención de 2026 (0.90%) e
    inflación derivada del INPC sembrado: de 6,180 brutos, 900 se van a
    impuestos, 3,365.98 se los come la inflación y quedan 1,914.02 reales.

    La inflación no es la constante redondeada 3.37 sino la que sale de dividir
    el INPC de junio de 2026 entre el de junio de 2025 — el sistema calcula con
    la serie, no con un número copiado.
    """
    cuerpo = await _calcular(
        api_lectura,
        monto="100000",
        plazo_dias=360,
        producto_ids=[await _producto_id("cetes-28")],
    )
    cascada = cuerpo["resultados"][0]["cascada"]

    assert cascada["rendimiento_bruto"] == "6180.00"
    assert cascada["isr_retenido"] == "900.00"
    assert cascada["rendimiento_neto"] == "5280.00"
    assert Decimal(cascada["efecto_inflacion"]) == Decimal("3365.98")
    assert Decimal(cascada["ganancia_real"]) == Decimal("1914.02")

    inflacion = Decimal(cascada["inflacion_anual"])
    assert Decimal("3.36") < inflacion < Decimal("3.37")

    # Menos de un tercio de lo que sugiere la tasa nominal.
    assert Decimal(cascada["ganancia_real"]) < Decimal(cascada["rendimiento_bruto"]) / 3


async def test_a_custom_inflation_scenario_can_be_simulated(
    api_lectura: AsyncClient,
) -> None:
    cascada = (
        await _calcular(
            api_lectura,
            monto="100000",
            plazo_dias=360,
            inflacion_anual="8.0",
            producto_ids=[await _producto_id("cetes-28")],
        )
    )["resultados"][0]["cascada"]

    assert cascada["inflacion_anual"] == "8.0"
    assert Decimal(cascada["ganancia_real"]) < 0


# ─── Plazo ────────────────────────────────────────────────────


async def test_the_product_term_is_used_by_default(api_lectura: AsyncClient) -> None:
    cuerpo = await _calcular(
        api_lectura, monto="100000", producto_ids=[await _producto_id("cetes-91")]
    )
    assert cuerpo["resultados"][0]["cascada"]["plazo_dias"] == 91


async def test_sight_products_annualize_over_a_full_year(
    api_lectura: AsyncClient,
) -> None:
    """No tienen plazo contractual, pero sí necesitan un horizonte."""
    cuerpo = await _calcular(
        api_lectura, monto="100000", producto_ids=[await _producto_id("bonddia")]
    )
    assert cuerpo["resultados"][0]["cascada"]["plazo_dias"] == 365


async def test_an_explicit_term_overrides_the_product_one(
    api_lectura: AsyncClient,
) -> None:
    cuerpo = await _calcular(
        api_lectura,
        monto="100000",
        plazo_dias=180,
        producto_ids=[await _producto_id("cetes-364")],
    )
    assert cuerpo["resultados"][0]["cascada"]["plazo_dias"] == 180


# ─── Cobertura y exposición ───────────────────────────────────


async def test_reports_how_much_would_be_unprotected(api_lectura: AsyncClient) -> None:
    """Comparar sólo por tasa ignoraría cuánto del dinero está protegido."""
    cuerpo = await _calcular(
        api_lectura, monto="500000", producto_ids=[await _producto_id("klar-vista")]
    )

    resultado = cuerpo["resultados"][0]
    assert Decimal(resultado["monto_expuesto"]) > Decimal("280000")
    assert resultado["cobertura"]["tipo"] == "PROSOFIPO"


async def test_sovereign_debt_leaves_nothing_exposed(api_lectura: AsyncClient) -> None:
    cuerpo = await _calcular(
        api_lectura, monto="50000000", producto_ids=[await _producto_id("cetes-28")]
    )
    assert cuerpo["resultados"][0]["monto_expuesto"] == "0.00"


async def test_an_ifpe_exposes_the_whole_amount(api_lectura: AsyncClient) -> None:
    cuerpo = await _calcular(
        api_lectura, monto="10000", producto_ids=[await _producto_id("mercado-pago-vista")]
    )
    assert cuerpo["resultados"][0]["monto_expuesto"] == "10000.00"


# ─── Comparación de varios productos ──────────────────────────


async def test_compares_several_products_in_the_requested_order(
    api_lectura: AsyncClient,
) -> None:
    """Se respeta el orden pedido: es el orden en que el usuario los lee."""
    ids = [
        await _producto_id("klar-vista"),
        await _producto_id("cetes-28"),
        await _producto_id("nu-cajita-turbo"),
    ]
    cuerpo = await _calcular(api_lectura, monto="100000", plazo_dias=360, producto_ids=ids)

    assert [r["producto_id"] for r in cuerpo["resultados"]] == ids


# ─── Tramos por saldo ─────────────────────────────────────────


async def test_a_tiered_product_blends_its_cascade(api_lectura: AsyncClient) -> None:
    """Ponderar primero, cascada después.

    A $50,000 la tasa que Openbank paga de verdad es (30,000 × 13 + 20,000 ×
    6.3) / 50,000 = 10.32%, y toda la cascada debe salir de ella para que los
    importes cuadren con la tasa mostrada.
    """
    cuerpo = await _calcular(
        api_lectura, monto="50000", producto_ids=[await _producto_id("openbank-vista")]
    )

    resultado = cuerpo["resultados"][0]
    assert resultado["escalonada"] is True
    assert len(resultado["tramos"]) == 2
    assert Decimal(resultado["cascada"]["tasa_nominal"]) == Decimal("10.3200")
    assert Decimal(resultado["cascada"]["ten"]) == Decimal("9.4200")


async def test_an_amount_inside_the_first_tier_pays_the_headline(
    api_lectura: AsyncClient,
) -> None:
    cuerpo = await _calcular(
        api_lectura, monto="20000", producto_ids=[await _producto_id("openbank-vista")]
    )
    assert Decimal(cuerpo["resultados"][0]["cascada"]["tasa_nominal"]) == Decimal("13.0000")


async def test_a_higher_nominal_rate_can_lose_to_a_lower_one(
    api_lectura: AsyncClient,
) -> None:
    """El caso que justifica el producto, extremo a extremo.

    Nu ofrece 13% y CETES 6.18%, pero la comparación honesta también incluye
    cuánto del dinero queda protegido: la cobertura de un monto grande difiere
    en dos órdenes de magnitud.
    """
    cuerpo = await _calcular(
        api_lectura,
        monto="5000000",
        plazo_dias=360,
        producto_ids=[
            await _producto_id("nu-cajita-turbo"),
            await _producto_id("cetes-28"),
        ],
    )

    nu, cete = cuerpo["resultados"]
    assert Decimal(nu["cascada"]["ganancia_real"]) > Decimal(cete["cascada"]["ganancia_real"])
    assert Decimal(nu["monto_expuesto"]) > Decimal("1000000")
    assert cete["monto_expuesto"] == "0.00"


# ─── Validación ───────────────────────────────────────────────


async def test_unknown_products_fail_loudly(api_lectura: AsyncClient) -> None:
    respuesta = await api_lectura.post(RUTA, json={"monto": "1000", "producto_ids": [999999]})
    assert respuesta.status_code == 404
    assert "inexistentes" in respuesta.json()["detail"]


@pytest.mark.usefixtures("solo_verificadas")
async def test_products_without_a_publishable_rate_fail_loudly(
    api_lectura: AsyncClient,
) -> None:
    """Omitirlos en silencio haría comparar peras con nada.

    Con la bandera de transición apagada una tasa pendiente no es publicable,
    y la calculadora lo dice en vez de calcular sobre ella.
    """
    respuesta = await api_lectura.post(
        RUTA,
        json={"monto": "1000", "producto_ids": [await _producto_id("finsus-plazo-28")]},
    )
    assert respuesta.status_code == 404
    assert "pendiente de verificación" in respuesta.json()["detail"]


async def test_an_unverified_rate_computes_while_the_transition_policy_is_on(
    api_lectura: AsyncClient,
) -> None:
    """El mismo criterio de publicabilidad que el comparador y la combinación.

    Sin la unificación, una fila «sin verificar» visible en el comparador
    respondía 404 al llegar aquí: tres endpoints, dos reglas.
    """
    cuerpo = await _calcular(
        api_lectura, monto="10000", producto_ids=[await _producto_id("hey-vista")]
    )

    resultado = cuerpo["resultados"][0]
    assert resultado["procedencia"]["verificada"] is False
    assert Decimal(resultado["cascada"]["rendimiento_bruto"]) > 0


async def test_a_non_positive_amount_is_rejected(api_lectura: AsyncClient) -> None:
    respuesta = await api_lectura.post(RUTA, json={"monto": "0", "producto_ids": [1]})
    assert respuesta.status_code == 422


async def test_an_empty_product_list_is_rejected(api_lectura: AsyncClient) -> None:
    respuesta = await api_lectura.post(RUTA, json={"monto": "1000", "producto_ids": []})
    assert respuesta.status_code == 422
