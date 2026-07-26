"""Tests de la calculadora de combinación y del optimizador.

El motor ya está probado en `tests/metrics/test_portfolio.py`; aquí se
comprueba lo que sólo se puede comprobar de punta a punta: que el catálogo
publicable es el mismo que el del comparador, que la respuesta trae lo que la
UI necesita, y que las advertencias obligatorias viajan siempre.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient

pytestmark = [
    pytest.mark.requires_docker,
    pytest.mark.usefixtures("comparador_poblado", "sin_modo_demo"),
]

COMBINACION = "/api/v1/calculadora/combinacion"
OPTIMIZAR = "/api/v1/calculadora/optimizar"


async def _producto_id(api: AsyncClient, slug: str) -> int:
    cuerpo = (await api.get("/api/v1/comparador")).json()
    return next(f["producto_id"] for f in cuerpo["filas"] if f["producto_slug"] == slug)


# ─── Autenticación ────────────────────────────────────────────


async def test_combination_requires_authentication(api: AsyncClient) -> None:
    respuesta = await api.post(
        COMBINACION,
        json={
            "monto_total": "100000",
            "horizonte_dias": 364,
            "items": [{"producto_id": 1, "porcentaje": "100"}],
        },
    )
    assert respuesta.status_code == 401


async def test_optimiser_requires_authentication(api: AsyncClient) -> None:
    respuesta = await api.post(OPTIMIZAR, json={"monto_total": "100000", "horizonte_dias": 364})
    assert respuesta.status_code == 401


# ─── Combinación ──────────────────────────────────────────────


async def test_returns_the_full_breakdown(api_lectura: AsyncClient) -> None:
    cetes = await _producto_id(api_lectura, "cetes-364")
    nu = await _producto_id(api_lectura, "nu-cajita-turbo")

    respuesta = await api_lectura.post(
        COMBINACION,
        json={
            "monto_total": "250000",
            "horizonte_dias": 364,
            "items": [
                {"producto_id": cetes, "porcentaje": "60"},
                {"producto_id": nu, "porcentaje": "40"},
            ],
        },
    )

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()

    assert len(cuerpo["asignaciones"]) == 2
    assert Decimal(cuerpo["monto_total"]) == Decimal("250000.00")
    assert cuerpo["horizonte_dias"] == 364
    assert cuerpo["narrativa"]
    assert cuerpo["nota_fiscal"]
    assert cuerpo["disclaimer"]
    assert cuerpo["aviso_optimizador"]


async def test_the_cascade_adds_up_in_the_response(api_lectura: AsyncClient) -> None:
    """El usuario suma los números de la pantalla."""
    cetes = await _producto_id(api_lectura, "cetes-364")
    nu = await _producto_id(api_lectura, "nu-cajita-turbo")

    cuerpo = (
        await api_lectura.post(
            COMBINACION,
            json={
                "monto_total": "250000",
                "horizonte_dias": 364,
                "items": [
                    {"producto_id": cetes, "porcentaje": "60"},
                    {"producto_id": nu, "porcentaje": "40"},
                ],
            },
        )
    ).json()

    bruto = Decimal(cuerpo["rendimiento_bruto"])
    assert bruto == (
        Decimal(cuerpo["isr_retenido"])
        + Decimal(cuerpo["efecto_inflacion"])
        + Decimal(cuerpo["ganancia_real"])
    )
    assert sum(Decimal(a["monto"]) for a in cuerpo["asignaciones"]) == Decimal(
        cuerpo["monto_total"]
    )


async def test_percentages_are_normalised_not_rejected(api_lectura: AsyncClient) -> None:
    """70 y 40 expresa una proporción, no un error de captura."""
    cetes = await _producto_id(api_lectura, "cetes-364")
    nu = await _producto_id(api_lectura, "nu-cajita-turbo")

    cuerpo = (
        await api_lectura.post(
            COMBINACION,
            json={
                "monto_total": "100000",
                "horizonte_dias": 364,
                "items": [
                    {"producto_id": cetes, "porcentaje": "70"},
                    {"producto_id": nu, "porcentaje": "40"},
                ],
            },
        )
    ).json()

    porcentajes = [Decimal(a["porcentaje"]) for a in cuerpo["asignaciones"]]
    assert sum(porcentajes) == Decimal("100")
    assert porcentajes == [Decimal("63.6"), Decimal("36.4")]


async def test_every_allocation_carries_provenance_and_coverage(
    api_lectura: AsyncClient,
) -> None:
    cetes = await _producto_id(api_lectura, "cetes-364")

    cuerpo = (
        await api_lectura.post(
            COMBINACION,
            json={
                "monto_total": "100000",
                "horizonte_dias": 364,
                "items": [{"producto_id": cetes, "porcentaje": "100"}],
            },
        )
    ).json()

    asignacion = cuerpo["asignaciones"][0]
    assert asignacion["procedencia"]["fecha_dato"]
    assert asignacion["procedencia"]["fuente"]
    assert asignacion["cobertura"]["sin_limite"] is True
    assert asignacion["institucion"]["nombre"] == "Gobierno Federal"


async def test_a_product_without_a_publishable_rate_is_rejected(
    api_lectura: AsyncClient,
) -> None:
    """El mismo catálogo que el comparador: sin puerta trasera."""
    respuesta = await api_lectura.post(
        COMBINACION,
        json={
            "monto_total": "100000",
            "horizonte_dias": 364,
            "items": [{"producto_id": 999999, "porcentaje": "100"}],
        },
    )

    assert respuesta.status_code == 404
    assert "999999" in respuesta.json()["detail"]


async def test_a_term_beyond_the_horizon_carries_a_warning(
    api_lectura: AsyncClient,
) -> None:
    cetes = await _producto_id(api_lectura, "cetes-364")

    cuerpo = (
        await api_lectura.post(
            COMBINACION,
            json={
                "monto_total": "100000",
                "horizonte_dias": 91,
                "items": [{"producto_id": cetes, "porcentaje": "100"}],
            },
        )
    ).json()

    assert cuerpo["asignaciones"][0]["advertencia_liquidez"] is not None


@pytest.mark.parametrize(
    "cuerpo",
    [
        {
            "monto_total": "0",
            "horizonte_dias": 364,
            "items": [{"producto_id": 1, "porcentaje": "100"}],
        },
        {
            "monto_total": "100",
            "horizonte_dias": 0,
            "items": [{"producto_id": 1, "porcentaje": "100"}],
        },
        {"monto_total": "100", "horizonte_dias": 364, "items": []},
        {
            "monto_total": "100",
            "horizonte_dias": 364,
            "items": [{"producto_id": 1, "porcentaje": "101"}],
        },
    ],
)
async def test_invalid_requests_are_rejected(
    api_lectura: AsyncClient, cuerpo: dict[str, object]
) -> None:
    assert (await api_lectura.post(COMBINACION, json=cuerpo)).status_code == 422


# ─── Optimizador ──────────────────────────────────────────────


async def test_the_optimiser_proposes_and_evaluates(api_lectura: AsyncClient) -> None:
    respuesta = await api_lectura.post(
        OPTIMIZAR, json={"monto_total": "250000", "horizonte_dias": 364}
    )

    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()

    assert cuerpo["asignaciones"], "debe proponer algo con el catálogo cargado"
    assert sum(Decimal(a["porcentaje"]) for a in cuerpo["asignaciones"]) == Decimal("100")
    assert Decimal(cuerpo["ten_ponderada"]) > 0


async def test_the_optimiser_respects_coverage_caps(api_lectura: AsyncClient) -> None:
    """El criterio de la fase, extremo a extremo."""
    cuerpo = (
        await api_lectura.post(
            OPTIMIZAR,
            json={"monto_total": "5000000", "horizonte_dias": 364, "respetar_seguro": True},
        )
    ).json()

    for asignacion in cuerpo["asignaciones"]:
        limite = asignacion["cobertura"]["limite_mxn"]
        if limite is not None:
            assert Decimal(asignacion["monto"]) <= Decimal(limite), asignacion["producto"]
    assert Decimal(cuerpo["porcentaje_protegido"]) == Decimal("100")


async def test_the_optimiser_leaves_out_uncovered_issuers(api_lectura: AsyncClient) -> None:
    """Mercado Pago paga bien y no tiene fondo de protección: no entra."""
    cuerpo = (
        await api_lectura.post(
            OPTIMIZAR,
            json={"monto_total": "250000", "horizonte_dias": 364, "respetar_seguro": True},
        )
    ).json()

    assert "mercado-pago-vista" not in {a["producto_slug"] for a in cuerpo["asignaciones"]}


async def test_without_the_insurance_toggle_everything_goes_to_the_best_rate(
    api_lectura: AsyncClient,
) -> None:
    """Sin tope que respetar, la heurística deja de repartir.

    Es coherente: los topes son la única razón por la que diversificar mejora
    algo en este modelo. Quitarlos convierte al optimizador en "todo al que más
    rinde", que es exactamente lo que la bandera del comparador desaconseja —
    y por eso el interruptor viene encendido por defecto.
    """
    cuerpo = (
        await api_lectura.post(
            OPTIMIZAR,
            json={"monto_total": "250000", "horizonte_dias": 364, "respetar_seguro": False},
        )
    ).json()

    assert len(cuerpo["asignaciones"]) == 1
    assert Decimal(cuerpo["asignaciones"][0]["porcentaje"]) == Decimal("100")


async def test_sovereign_debt_absorbs_whatever_does_not_fit(
    api_lectura: AsyncClient,
) -> None:
    """Con este catálogo nunca sobra dinero, y por una razón concreta.

    Siempre hay deuda gubernamental disponible —BONDDIA es a la vista, así que
    entra en cualquier horizonte— y no tiene tope de cobertura. Medio millón de
    pesos o quinientos millones acaban colocados igual: los emisores con seguro
    se llenan hasta su límite y el soberano absorbe el resto.

    El caso contrario —agotar la cobertura y que sobre dinero— sí existe en el
    motor y está probado ahí; con el catálogo real no es alcanzable.
    """
    cuerpo = (
        await api_lectura.post(
            OPTIMIZAR,
            json={"monto_total": "500000000", "horizonte_dias": 364, "respetar_seguro": True},
        )
    ).json()

    assert Decimal(cuerpo["monto_no_asignado"]) == Decimal("0.00")
    assert Decimal(cuerpo["monto_total"]) == Decimal("500000000.00")
    assert Decimal(cuerpo["porcentaje_protegido"]) == Decimal("100")

    soberanos = [a for a in cuerpo["asignaciones"] if a["cobertura"]["sin_limite"]]
    assert soberanos, "el reparto tiene que apoyarse en el soberano"
    assert Decimal(soberanos[0]["monto"]) > Decimal("400000000")


async def test_the_optimiser_always_warns_that_it_is_a_heuristic(
    api_lectura: AsyncClient,
) -> None:
    """§10 y §19: no es una recomendación, y se dice en la respuesta."""
    cuerpo = (
        await api_lectura.post(OPTIMIZAR, json={"monto_total": "250000", "horizonte_dias": 364})
    ).json()

    assert "no es una recomendación de inversión" in cuerpo["aviso_optimizador"].lower()
    assert cuerpo["disclaimer"]


async def test_the_response_echoes_the_calculation_context(api_lectura: AsyncClient) -> None:
    cuerpo = (
        await api_lectura.post(OPTIMIZAR, json={"monto_total": "250000", "horizonte_dias": 364})
    ).json()

    assert Decimal(cuerpo["valor_udi"]) > 0
    assert Decimal(cuerpo["inflacion_anual"]) > 0
