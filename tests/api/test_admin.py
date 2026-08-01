"""Tests de los endpoints de administración."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from core.db import session_scope
from domain.enums import EstadoRevision, EstadoTasa
from domain.orm import Producto, RevisionTasa, Tasa

#: `solo_verificadas` porque lo que se prueba aquí es el pipeline de
#: publicación: qué llega al comparador y qué no. Con la bandera de transición
#: encendida, una tasa pendiente aparece por diseño y ya no se distinguiría de
#: una que llegó por aprobación.
pytestmark = [
    pytest.mark.requires_docker,
    pytest.mark.usefixtures("comparador_poblado", "solo_verificadas"),
]


async def _producto_id(slug: str) -> int:
    async with session_scope() as session:
        producto_id = await session.scalar(select(Producto.id).where(Producto.slug == slug))
    assert producto_id is not None
    return producto_id


async def _crear_revision(slug: str = "finsus-plazo-28") -> tuple[int, int]:
    """Deja una tasa pendiente con su revisión, como hará la fase 9."""
    async with session_scope() as session:
        producto_id = await session.scalar(select(Producto.id).where(Producto.slug == slug))
        tasa = Tasa(
            producto_id=producto_id,
            tasa_nominal=Decimal("11.50"),
            fecha_dato=date(2026, 7, 22),
            fuente="LLM_RESEARCH",
            estado=EstadoTasa.PENDIENTE_REVISION,
        )
        session.add(tasa)
        await session.flush()
        revision = RevisionTasa(
            tasa_id=tasa.id,
            motivo="Se aleja 4pp de la tasa vigente",
            valor_anterior=Decimal("7.19"),
            valor_nuevo=Decimal("11.50"),
        )
        session.add(revision)
        await session.flush()
        return revision.id, tasa.id


# ─── Autorización ─────────────────────────────────────────────


async def test_write_requires_the_admin_key(api_lectura: AsyncClient) -> None:
    """La llave del BFF puede leer el comparador pero no dar de alta tasas."""
    respuesta = await api_lectura.post(
        "/admin/tasas",
        json={
            "producto_id": await _producto_id("cetes-28"),
            "tasa_nominal": "6.20",
            "fecha_dato": "2026-07-22",
        },
    )
    assert respuesta.status_code == 403


async def test_no_key_is_rejected(api: AsyncClient) -> None:
    assert (await api.get("/admin/revisiones")).status_code == 401


# ─── Alta de tasas ────────────────────────────────────────────


async def test_creates_a_rate(api_admin: AsyncClient) -> None:
    respuesta = await api_admin.post(
        "/admin/tasas",
        json={
            "producto_id": await _producto_id("cetes-28"),
            "tasa_nominal": "6.20",
            "fecha_dato": "2026-07-22",
            "fuente_url": "https://banxico.org.mx/x",
        },
    )

    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert cuerpo["tasa_nominal"] == "6.2000"
    assert cuerpo["estado"] == "VIGENTE"


async def test_a_new_rate_becomes_the_current_one(api_admin: AsyncClient) -> None:
    """Append-only: la anterior sigue ahí, pero deja de ser la vigente."""
    producto_id = await _producto_id("cetes-28")
    await api_admin.post(
        "/admin/tasas",
        json={
            "producto_id": producto_id,
            "tasa_nominal": "6.05",
            "fecha_dato": date.today().isoformat(),
        },
    )

    cuerpo = (await api_admin.get("/api/v1/comparador", params={"plazo": "28"})).json()
    cete = next(f for f in cuerpo["filas"] if f["producto_slug"] == "cetes-28")
    assert cete["tasa_nominal"] == "6.0500"

    async with session_scope() as session:
        total = len(
            (await session.execute(select(Tasa).where(Tasa.producto_id == producto_id)))
            .scalars()
            .all()
        )
    assert total >= 2


async def test_duplicate_observation_is_rejected(api_admin: AsyncClient) -> None:
    """Reintentar no puede crear dos observaciones del mismo hecho."""
    payload = {
        "producto_id": await _producto_id("cetes-91"),
        "tasa_nominal": "6.49",
        "fecha_dato": "2026-07-22",
    }
    assert (await api_admin.post("/admin/tasas", json=payload)).status_code == 201

    repetida = await api_admin.post("/admin/tasas", json=payload)
    assert repetida.status_code == 409
    assert "Ya existe" in repetida.json()["detail"]


async def test_unknown_product_is_rejected(api_admin: AsyncClient) -> None:
    respuesta = await api_admin.post(
        "/admin/tasas",
        json={"producto_id": 999999, "tasa_nominal": "6.20", "fecha_dato": "2026-07-22"},
    )
    assert respuesta.status_code == 404


async def test_future_dated_rate_is_rejected(api_admin: AsyncClient) -> None:
    respuesta = await api_admin.post(
        "/admin/tasas",
        json={
            "producto_id": await _producto_id("cetes-28"),
            "tasa_nominal": "6.20",
            "fecha_dato": (date.today() + timedelta(days=10)).isoformat(),
        },
    )
    assert respuesta.status_code == 422


async def test_implausible_rate_is_rejected(api_admin: AsyncClient) -> None:
    respuesta = await api_admin.post(
        "/admin/tasas",
        json={
            "producto_id": await _producto_id("cetes-28"),
            "tasa_nominal": "950",
            "fecha_dato": "2026-07-22",
        },
    )
    assert respuesta.status_code == 422


# ─── Cola de revisión ─────────────────────────────────────────


async def test_empty_queue_returns_an_empty_list(api_admin: AsyncClient) -> None:
    respuesta = await api_admin.get("/admin/revisiones")
    assert respuesta.status_code == 200
    assert respuesta.json() == []


async def test_the_queue_names_the_institution_and_product(
    api_admin: AsyncClient,
) -> None:
    """Una cola que sólo muestre ids obliga a buscar a mano para decidir."""
    await _crear_revision()

    fila = (await api_admin.get("/admin/revisiones")).json()[0]
    assert fila["institucion"] == "Finsus"
    assert fila["producto"] == "Finsus Plazo 28 días"
    assert fila["valor_anterior"] == "7.1900"
    assert fila["valor_nuevo"] == "11.5000"
    assert fila["estado"] == "PENDIENTE"


async def test_approving_publishes_the_rate(api_admin: AsyncClient) -> None:
    revision_id, tasa_id = await _crear_revision()

    respuesta = await api_admin.post(
        f"/admin/revisiones/{revision_id}",
        json={"aprobar": True, "revisor": "gibran", "comentario": "verificado en el sitio"},
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["estado"] == "APROBADA"

    async with session_scope() as session:
        tasa = await session.get(Tasa, tasa_id)
    assert tasa is not None and tasa.estado is EstadoTasa.VIGENTE


async def test_rejecting_discards_the_rate_without_deleting_it(
    api_admin: AsyncClient,
) -> None:
    """No se borra nada: la decisión queda registrada con quién la tomó."""
    revision_id, tasa_id = await _crear_revision()

    respuesta = await api_admin.post(
        f"/admin/revisiones/{revision_id}",
        json={"aprobar": False, "revisor": "gibran", "comentario": "no coincide con la fuente"},
    )

    assert respuesta.json()["estado"] == "RECHAZADA"

    async with session_scope() as session:
        tasa = await session.get(Tasa, tasa_id)
        revision = await session.get(RevisionTasa, revision_id)

    assert tasa is not None and tasa.estado is EstadoTasa.RECHAZADA
    assert revision is not None
    assert revision.revisor == "gibran"
    assert revision.resuelto_at is not None
    assert "no coincide con la fuente" in revision.motivo


async def test_a_rejected_rate_never_reaches_the_comparator(
    api_admin: AsyncClient,
) -> None:
    revision_id, _ = await _crear_revision()
    await api_admin.post(
        f"/admin/revisiones/{revision_id}", json={"aprobar": False, "revisor": "gibran"}
    )

    cuerpo = (await api_admin.get("/api/v1/comparador", params={"plazo": "28"})).json()
    assert "finsus-plazo-28" not in {f["producto_slug"] for f in cuerpo["filas"]}


async def test_an_approved_rate_reaches_the_comparator(api_admin: AsyncClient) -> None:
    """El flujo completo de §15, extremo a extremo."""
    revision_id, _ = await _crear_revision()
    await api_admin.post(
        f"/admin/revisiones/{revision_id}", json={"aprobar": True, "revisor": "gibran"}
    )

    cuerpo = (await api_admin.get("/api/v1/comparador", params={"plazo": "28"})).json()
    finsus = next(f for f in cuerpo["filas"] if f["producto_slug"] == "finsus-plazo-28")
    assert finsus["tasa_nominal"] == "11.5000"


async def test_resolving_twice_is_rejected(api_admin: AsyncClient) -> None:
    """Sobrescribiría quién decidió y cuándo."""
    revision_id, _ = await _crear_revision()
    await api_admin.post(
        f"/admin/revisiones/{revision_id}", json={"aprobar": True, "revisor": "gibran"}
    )

    segunda = await api_admin.post(
        f"/admin/revisiones/{revision_id}", json={"aprobar": False, "revisor": "otro"}
    )
    assert segunda.status_code == 409


async def test_resolved_reviews_can_be_listed_by_state(api_admin: AsyncClient) -> None:
    revision_id, _ = await _crear_revision()
    await api_admin.post(
        f"/admin/revisiones/{revision_id}", json={"aprobar": True, "revisor": "gibran"}
    )

    assert (await api_admin.get("/admin/revisiones")).json() == []
    aprobadas = (
        await api_admin.get("/admin/revisiones", params={"estado": EstadoRevision.APROBADA.value})
    ).json()
    assert len(aprobadas) == 1


async def test_unknown_review_returns_404(api_admin: AsyncClient) -> None:
    respuesta = await api_admin.post(
        "/admin/revisiones/99999", json={"aprobar": True, "revisor": "gibran"}
    )
    assert respuesta.status_code == 404


async def test_reviewer_is_required(api_admin: AsyncClient) -> None:
    """Sin revisor, el historial no sirve para auditar."""
    revision_id, _ = await _crear_revision()
    respuesta = await api_admin.post(f"/admin/revisiones/{revision_id}", json={"aprobar": True})
    assert respuesta.status_code == 422
