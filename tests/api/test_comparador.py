"""Tests del comparador.

Cada filtro de §7 tiene un test que verifica **inclusión y exclusión**: que lo
que debe aparecer aparece y que lo que debe quedar fuera queda fuera. Un filtro
que no excluye nada pasaría igual de bien un test que sólo mire lo incluido.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from core.db import session_scope
from domain.enums import Severidad, TipoBandera
from domain.orm import Bandera, Institucion

#: `real_redis` da un Redis efímero y vacío por test. Sin él, el cache del
#: comparador serviría a un test la respuesta que dejó el anterior.
#:
#: `sin_modo_demo` apaga el modo demostración para todo el módulo: los tests de
#: filtros afirman conjuntos exactos, y con el modo encendido entran las 30
#: tasas sin verificar del seed y el catálogo deja de ser estable. Lo que se
#: prueba aquí es que cada filtro incluya y excluya lo que debe, no cuántas
#: filas hay. El modo demo tiene su propia sección, que lo enciende.
pytestmark = [
    pytest.mark.requires_docker,
    pytest.mark.usefixtures("comparador_poblado", "real_redis", "sin_modo_demo"),
]

RUTA = "/api/v1/comparador"

#: Lo único verificado contra fuente primaria: deuda gubernamental leída del
#: SIE de Banxico y de cetesdirecto.
SLUGS_VERIFICADOS = frozenset({"cetes-28", "cetes-91", "cetes-182", "cetes-364", "bonddia"})

#: Las dos instituciones ficticias (◆), que existen para que el motor de
#: banderas tenga casos que evaluar mientras la CNBV no llega (fase 8).
SLUGS_DEMO = frozenset({"ahorra-mas-plazo-364", "alcancia-plazo-182"})

#: Todo lo que el seed publica.
SLUGS_DEL_SEED = SLUGS_VERIFICADOS | SLUGS_DEMO

#: Tasas VIGENTE que añade `comparador_poblado` sobre instituciones reales,
#: para que cada filtro de §7 tenga algo que incluir y algo que excluir.
SLUGS_FIXTURE = frozenset(
    {
        "finsus-plazo-91",
        "klar-vista",
        "nu-cajita-turbo",
        "nu-plazo-91",
        "mercado-pago-vista",
        "libertad-plazo-364",
    }
)


async def _slugs(api: AsyncClient, **params: object) -> set[str]:
    respuesta = await api.get(RUTA, params=params)
    assert respuesta.status_code == 200, respuesta.text
    return {fila["producto_slug"] for fila in respuesta.json()["filas"]}


async def _marcar(nombre: str) -> None:
    async with session_scope() as session:
        institucion = await session.scalar(select(Institucion).where(Institucion.nombre == nombre))
        assert institucion is not None
        session.add(
            Bandera(
                institucion_id=institucion.id,
                tipo=TipoBandera.IMOR,
                severidad=Severidad.ROJA,
                motivo="Morosidad del 9%",
                periodo_dato=date(2026, 3, 31),
                activa=True,
            )
        )


# ─── Básico ───────────────────────────────────────────────────


async def test_requires_authentication(api: AsyncClient) -> None:
    assert (await api.get(RUTA)).status_code == 401


async def test_returns_only_publishable_rates(api_lectura: AsyncClient) -> None:
    """El catálogo tiene 42 productos; sólo salen los que tienen tasa vigente."""
    assert await _slugs(api_lectura) == SLUGS_VERIFICADOS | SLUGS_FIXTURE


# ─── Modo demostración ────────────────────────────────────────


@pytest.mark.usefixtures("con_modo_demo")
async def test_demo_mode_adds_the_illustrative_and_unverified_rows(
    api_lectura: AsyncClient,
) -> None:
    slugs = await _slugs(api_lectura)

    assert SLUGS_DEMO <= slugs
    assert "hey-vista" in slugs  # institución real con tasa sin confirmar
    assert SLUGS_VERIFICADOS | SLUGS_FIXTURE <= slugs


@pytest.mark.usefixtures("con_modo_demo")
async def test_demo_rows_are_always_marked_as_such(api_lectura: AsyncClient) -> None:
    """Se amplía lo que se muestra, nunca lo que se afirma."""
    cuerpo = (await api_lectura.get(RUTA)).json()
    por_slug = {f["producto_slug"]: f for f in cuerpo["filas"]}

    # Institución ficticia: la marca va en la institución.
    assert por_slug["alcancia-plazo-182"]["institucion"]["es_demostracion"] is True
    # ...y su tasa sí es "verificada" en el sentido del estado: no hay fuente
    # oficial que contradecirla porque la institución no existe. Lo que avisa
    # al usuario es el ◆, no la procedencia.
    assert por_slug["alcancia-plazo-182"]["procedencia"]["verificada"] is True

    # Institución real con tasa sin confirmar: al revés exactamente.
    assert por_slug["hey-vista"]["institucion"]["es_demostracion"] is False
    assert por_slug["hey-vista"]["procedencia"]["verificada"] is False
    assert por_slug["hey-vista"]["procedencia"]["estado"] == "PENDIENTE_REVISION"

    # Y lo verificado de verdad no lleva ninguna de las dos marcas.
    assert por_slug["cetes-28"]["institucion"]["es_demostracion"] is False
    assert por_slug["cetes-28"]["procedencia"]["verificada"] is True


async def test_turning_the_switch_off_serves_only_verified_data(
    api_lectura: AsyncClient,
) -> None:
    """El paso 9 de la fase 6: no basta con no marcar, hay que no servir.

    Queda lo verificado del seed más las tasas VIGENTE que añade la fixture
    sobre instituciones reales. Desaparecen las ficticias y las 30 que el seed
    dejó en PENDIENTE_REVISION.
    """
    slugs = await _slugs(api_lectura)

    assert slugs == SLUGS_VERIFICADOS | SLUGS_FIXTURE
    assert not slugs & SLUGS_DEMO


async def test_with_the_switch_off_no_row_is_unverified(
    api_lectura: AsyncClient,
) -> None:
    cuerpo = (await api_lectura.get(RUTA)).json()

    assert cuerpo["filas"]
    assert all(f["procedencia"]["verificada"] for f in cuerpo["filas"])
    assert not any(f["institucion"]["es_demostracion"] for f in cuerpo["filas"])


@pytest.mark.usefixtures("con_modo_demo")
async def test_an_unverified_rate_never_supersedes_a_verified_one(
    api_lectura: AsyncClient,
) -> None:
    """Precedencia por estado antes que por fecha.

    Sin ella, una observación pendiente más reciente ocultaría el dato bueno
    del mismo producto — justo lo contrario de lo que el modo demo busca.

    Tiene que correr con el modo **encendido**: apagado, la tasa pendiente ni
    siquiera es candidata y el test pasaría sin comprobar nada.
    """
    from datetime import timedelta

    from domain.enums import EstadoTasa, FuenteTasa
    from domain.orm import Producto, Tasa

    async with session_scope() as session:
        producto_id = await session.scalar(select(Producto.id).where(Producto.slug == "cetes-28"))
        assert producto_id is not None
        session.add(
            Tasa(
                producto_id=producto_id,
                tasa_nominal=Decimal("99.00"),
                fecha_dato=date.today() + timedelta(days=0),
                fuente=FuenteTasa.LLM_RESEARCH,
                estado=EstadoTasa.PENDIENTE_REVISION,
            )
        )

    cuerpo = (await api_lectura.get(RUTA)).json()
    cetes = next(f for f in cuerpo["filas"] if f["producto_slug"] == "cetes-28")

    assert Decimal(cetes["tasa_nominal"]) == Decimal("6.18")
    assert cetes["procedencia"]["verificada"] is True


async def test_every_row_carries_provenance(api_lectura: AsyncClient) -> None:
    """§11 y §19: ninguna tasa sin fecha ni fuente."""
    cuerpo = (await api_lectura.get(RUTA)).json()

    assert cuerpo["filas"]
    for fila in cuerpo["filas"]:
        assert fila["procedencia"]["fecha_dato"]
        assert fila["procedencia"]["fuente"]


async def test_response_echoes_the_calculation_context(api_lectura: AsyncClient) -> None:
    cuerpo = (await api_lectura.get(RUTA)).json()

    assert Decimal(cuerpo["valor_udi"]) > 0
    assert cuerpo["tasa_retencion_capital"] == "0.9000"
    assert cuerpo["disclaimer"]
    assert cuerpo["total"] == len(cuerpo["filas"])


# ─── Filtro: plazo ────────────────────────────────────────────


async def test_sight_filter_includes_and_excludes(api_lectura: AsyncClient) -> None:
    slugs = await _slugs(api_lectura, plazo="VISTA")
    assert "bonddia" in slugs
    assert "klar-vista" in slugs
    assert "cetes-28" not in slugs
    assert "finsus-plazo-91" not in slugs


async def test_term_filter_includes_and_excludes(api_lectura: AsyncClient) -> None:
    slugs = await _slugs(api_lectura, plazo="91")
    assert slugs == {"cetes-91", "finsus-plazo-91", "nu-plazo-91"}
    assert "cetes-28" not in slugs


async def test_one_year_filter_includes_and_excludes(api_lectura: AsyncClient) -> None:
    """§7 distingue "1 año" (364 días, el plazo de CETES) de "más de 1 año"."""
    slugs = await _slugs(api_lectura, plazo="364")
    assert slugs == {"cetes-364", "libertad-plazo-364"}
    assert "cetes-182" not in slugs


async def test_long_term_filter_excludes_one_year_products(
    api_lectura: AsyncClient,
) -> None:
    """365+ es estrictamente más de un año, así que 364 días queda fuera.

    El catálogo del MVP no llega a plazos mayores —BONOS M y UDIBONOS son de la
    fase 10— y el filtro devuelve vacío, que es la respuesta correcta.
    """
    slugs = await _slugs(api_lectura, plazo="365+")
    assert slugs == set()


async def test_invalid_term_fails_instead_of_returning_nothing(
    api_lectura: AsyncClient,
) -> None:
    """Una lista vacía parecería "no hay nada" y ocultaría el error."""
    respuesta = await api_lectura.get(RUTA, params={"plazo": "45"})
    assert respuesta.status_code == 422
    assert "Valores aceptados" in respuesta.json()["detail"]


# ─── Filtro: categoría ────────────────────────────────────────


async def test_category_filter_includes_and_excludes(api_lectura: AsyncClient) -> None:
    slugs = await _slugs(api_lectura, categoria="SOFIPO")
    assert slugs == {"finsus-plazo-91", "klar-vista", "libertad-plazo-364"}
    assert "cetes-28" not in slugs
    assert "nu-cajita-turbo" not in slugs


async def test_bank_category_excludes_sofipos(api_lectura: AsyncClient) -> None:
    slugs = await _slugs(api_lectura, categoria="BANCO_DIGITAL")
    assert slugs == {"nu-cajita-turbo", "nu-plazo-91"}


async def test_unknown_category_is_rejected(api_lectura: AsyncClient) -> None:
    respuesta = await api_lectura.get(RUTA, params={"categoria": "COOPERATIVA"})
    assert respuesta.status_code == 422


# ─── Filtro: monto ────────────────────────────────────────────


async def test_amount_filter_includes_and_excludes(api_lectura: AsyncClient) -> None:
    """Con $500 no se puede contratar lo que pide mínimo $1,000."""
    slugs = await _slugs(api_lectura, monto="500")
    assert "cetes-28" in slugs  # mínimo 100
    assert "klar-vista" in slugs  # mínimo 0
    assert "libertad-plazo-364" not in slugs  # mínimo 1000


async def test_a_larger_amount_includes_everything(api_lectura: AsyncClient) -> None:
    assert "libertad-plazo-364" in await _slugs(api_lectura, monto="100000")


async def test_amount_below_every_minimum_leaves_only_free_products(
    api_lectura: AsyncClient,
) -> None:
    slugs = await _slugs(api_lectura, monto="1")
    assert slugs == {"klar-vista", "nu-cajita-turbo", "nu-plazo-91", "mercado-pago-vista"}
    # Todo lo que exige un mínimo queda fuera, incluidos los CETES.
    assert not (slugs & SLUGS_DEL_SEED)


async def test_non_positive_amount_is_rejected(api_lectura: AsyncClient) -> None:
    assert (await api_lectura.get(RUTA, params={"monto": "0"})).status_code == 422


# ─── Filtro: seguro ───────────────────────────────────────────


async def test_ipab_only_includes_and_excludes(api_lectura: AsyncClient) -> None:
    slugs = await _slugs(api_lectura, seguro="solo_ipab")
    assert slugs == {"nu-cajita-turbo", "nu-plazo-91"}
    assert "finsus-plazo-91" not in slugs


async def test_government_only_includes_and_excludes(api_lectura: AsyncClient) -> None:
    slugs = await _slugs(api_lectura, seguro="solo_gobierno")
    # Los gubernamentales son hoy exactamente los verificados: lo que se pudo
    # confirmar contra fuente primaria fue el SIE de Banxico y cetesdirecto.
    assert slugs == set(SLUGS_VERIFICADOS)
    assert "klar-vista" not in slugs
    assert not slugs & SLUGS_DEMO


async def test_with_coverage_excludes_only_the_unprotected(
    api_lectura: AsyncClient,
) -> None:
    """El IFPE es el único sin fondo de protección."""
    slugs = await _slugs(api_lectura, seguro="con_cobertura")
    assert "mercado-pago-vista" not in slugs
    assert "klar-vista" in slugs
    assert "cetes-28" in slugs


async def test_all_includes_the_unprotected(api_lectura: AsyncClient) -> None:
    assert "mercado-pago-vista" in await _slugs(api_lectura, seguro="todos")


# ─── Filtro: liquidez ─────────────────────────────────────────


async def test_immediate_liquidity_includes_and_excludes(
    api_lectura: AsyncClient,
) -> None:
    slugs = await _slugs(api_lectura, liquidez="INMEDIATA")
    assert "bonddia" in slugs
    assert "klar-vista" in slugs
    assert "cetes-28" not in slugs


async def test_maturity_liquidity_includes_and_excludes(api_lectura: AsyncClient) -> None:
    slugs = await _slugs(api_lectura, liquidez="AL_VENCIMIENTO")
    assert "cetes-28" in slugs
    assert "bonddia" not in slugs


# ─── Filtro: sin banderas ─────────────────────────────────────


async def test_flagged_institutions_are_visible_by_default(
    api_lectura: AsyncClient,
) -> None:
    """§11: las banderas alertan, no descalifican por defecto."""
    await _marcar("Finsus")
    cuerpo = (await api_lectura.get(RUTA)).json()

    finsus = next(f for f in cuerpo["filas"] if f["producto_slug"] == "finsus-plazo-91")
    assert [b["tipo"] for b in finsus["banderas"]] == ["IMOR"]


async def test_without_flags_filter_includes_and_excludes(
    api_lectura: AsyncClient,
) -> None:
    await _marcar("Finsus")

    slugs = await _slugs(api_lectura, sin_banderas=True)
    assert "finsus-plazo-91" not in slugs
    assert "cetes-28" in slugs
    assert "klar-vista" in slugs


async def test_the_filter_excludes_every_product_of_the_flagged_institution(
    api_lectura: AsyncClient,
) -> None:
    """La bandera es de la institución, así que arrastra a todos sus productos."""
    await _marcar("Nu México")

    slugs = await _slugs(api_lectura, sin_banderas=True)
    assert "nu-cajita-turbo" not in slugs
    assert "nu-plazo-91" not in slugs


# ─── Orden ────────────────────────────────────────────────────


async def _orden(api: AsyncClient, **params: object) -> list[str]:
    respuesta = await api.get(RUTA, params=params)
    return [f["producto_slug"] for f in respuesta.json()["filas"]]


async def test_orders_by_net_effective_rate_by_default(api_lectura: AsyncClient) -> None:
    cuerpo = (await api_lectura.get(RUTA)).json()
    tens = [Decimal(f["ten"]) for f in cuerpo["filas"]]
    assert tens == sorted(tens, reverse=True)


async def test_orders_by_nominal_rate(api_lectura: AsyncClient) -> None:
    cuerpo = (await api_lectura.get(RUTA, params={"orden": "tasa_nominal"})).json()
    tasas = [Decimal(f["tasa_nominal"]) for f in cuerpo["filas"]]
    assert tasas == sorted(tasas, reverse=True)
    assert cuerpo["filas"][0]["producto_slug"] == "nu-cajita-turbo"


async def test_ascending_order_is_supported(api_lectura: AsyncClient) -> None:
    cuerpo = (
        await api_lectura.get(RUTA, params={"orden": "tasa_nominal", "descendente": False})
    ).json()
    tasas = [Decimal(f["tasa_nominal"]) for f in cuerpo["filas"]]
    assert tasas == sorted(tasas)


async def test_coverage_order_puts_sovereign_debt_first(
    api_lectura: AsyncClient,
) -> None:
    """Sin límite es más cobertura, no menos: no puede hundirse al final."""
    orden = await _orden(api_lectura, orden="cobertura")
    # Arriba, la deuda soberana (sin límite); abajo del todo, el IFPE, que no
    # tiene fondo de protección de ningún tipo.
    assert set(orden[: len(SLUGS_VERIFICADOS)]) == set(SLUGS_VERIFICADOS)
    assert orden[-1] == "mercado-pago-vista"


async def test_gat_order_falls_back_to_the_computed_equivalent(
    api_lectura: AsyncClient,
) -> None:
    cuerpo = (await api_lectura.get(RUTA, params={"orden": "gat"})).json()

    gats = [Decimal(f["gat"]["nominal"]) for f in cuerpo["filas"]]
    assert gats == sorted(gats, reverse=True)
    # Ninguna institución real publica GAT todavía: todas calculadas, y cada
    # fila lo declara.
    assert all(f["gat"]["es_calculada"] for f in cuerpo["filas"])


@pytest.mark.usefixtures("con_modo_demo")
async def test_gat_order_uses_the_published_value_when_there_is_one(
    api_lectura: AsyncClient,
) -> None:
    """El único producto del catálogo con GAT publicada es el ilustrativo.

    Existe justamente por eso: sin él, esta rama del `resolver_gat` no tendría
    ningún dato real que la ejercitara de punta a punta.
    """
    cuerpo = (await api_lectura.get(RUTA, params={"orden": "gat"})).json()
    por_slug = {f["producto_slug"]: f["gat"] for f in cuerpo["filas"]}

    ahorra = por_slug["ahorra-mas-plazo-364"]
    assert ahorra["origen"] == "PUBLICADA"
    assert ahorra["es_calculada"] is False
    assert Decimal(ahorra["nominal"]) == Decimal("18.40")

    gats = [Decimal(f["gat"]["nominal"]) for f in cuerpo["filas"]]
    assert gats == sorted(gats, reverse=True)
    assert all(por_slug[slug]["es_calculada"] for slug in SLUGS_VERIFICADOS)


async def test_ordering_is_stable_for_equal_values(api_lectura: AsyncClient) -> None:
    """Dos peticiones idénticas devuelven el mismo orden exacto."""
    primero = await _orden(api_lectura, orden="ten")
    segundo = await _orden(api_lectura, orden="ten")
    assert primero == segundo


# ─── Combinaciones ────────────────────────────────────────────


async def test_filters_combine(api_lectura: AsyncClient) -> None:
    slugs = await _slugs(api_lectura, categoria="SOFIPO", plazo="VISTA", monto="100")
    assert slugs == {"klar-vista"}


async def test_a_combination_with_no_matches_returns_an_empty_list(
    api_lectura: AsyncClient,
) -> None:
    respuesta = await api_lectura.get(
        RUTA, params={"categoria": "BANCO_TRADICIONAL", "plazo": "28"}
    )
    assert respuesta.status_code == 200
    assert respuesta.json() == {**respuesta.json(), "total": 0, "filas": []}


async def test_metrics_are_computed_per_row(api_lectura: AsyncClient) -> None:
    """La TEN de cada fila descuenta la retención vigente."""
    cuerpo = (await api_lectura.get(RUTA, params={"plazo": "28"})).json()

    cete = next(f for f in cuerpo["filas"] if f["producto_slug"] == "cetes-28")
    assert cete["tasa_nominal"] == "6.1800"
    assert cete["ten"] == "5.2800"
