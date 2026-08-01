"""Tests de autenticación y del contexto de cálculo."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api.dependencies import AdminDep, ContextoMercado, LecturaDep, umbrales_desde_config
from core.settings import settings

READ_KEY = settings.api_read_key.get_secret_value()
ADMIN_KEY = settings.api_admin_key.get_secret_value()


def _app_de_prueba() -> FastAPI:
    """App mínima con una ruta por nivel de permiso."""
    app = FastAPI()

    @app.get("/lectura")
    async def lectura(nivel: LecturaDep) -> dict[str, str]:
        return {"nivel": nivel}

    @app.post("/escritura")
    async def escritura(nivel: AdminDep) -> dict[str, str]:
        return {"nivel": nivel}

    return app


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=_app_de_prueba())
    return AsyncClient(transport=transport, base_url="http://test")


# ─── Autenticación ────────────────────────────────────────────


async def test_no_key_is_rejected(client: AsyncClient) -> None:
    for ruta, metodo in (("/lectura", "get"), ("/escritura", "post")):
        respuesta = await getattr(client, metodo)(ruta)
        assert respuesta.status_code == 401


async def test_wrong_key_is_rejected(client: AsyncClient) -> None:
    respuesta = await client.get("/lectura", headers={"X-API-Key": "no-es-la-llave"})
    assert respuesta.status_code == 401


async def test_read_key_can_read(client: AsyncClient) -> None:
    respuesta = await client.get("/lectura", headers={"X-API-Key": READ_KEY})
    assert respuesta.status_code == 200
    assert respuesta.json() == {"nivel": "lectura"}


async def test_read_key_cannot_write(client: AsyncClient) -> None:
    """403 y no 401: la credencial es válida, la operación no le corresponde.

    Devolver 401 llevaría al cliente a reintentar con la misma llave.
    """
    respuesta = await client.post("/escritura", headers={"X-API-Key": READ_KEY})
    assert respuesta.status_code == 403


async def test_admin_key_can_write(client: AsyncClient) -> None:
    respuesta = await client.post("/escritura", headers={"X-API-Key": ADMIN_KEY})
    assert respuesta.status_code == 200
    assert respuesta.json() == {"nivel": "admin"}


async def test_admin_key_can_also_read(client: AsyncClient) -> None:
    """El admin puede todo lo que puede el BFF; lo contrario no."""
    respuesta = await client.get("/lectura", headers={"X-API-Key": ADMIN_KEY})
    assert respuesta.status_code == 200
    assert respuesta.json() == {"nivel": "admin"}


async def test_an_empty_key_never_authenticates(client: AsyncClient) -> None:
    """Con la llave sin configurar, un header vacío no puede colarse."""
    respuesta = await client.get("/lectura", headers={"X-API-Key": ""})
    assert respuesta.status_code == 401


# ─── Umbrales desde ConfigStore ───────────────────────────────


def test_thresholds_are_built_from_the_config_store() -> None:
    """El único punto donde flags.py se conecta con la configuración."""
    umbrales = umbrales_desde_config()

    assert umbrales.imor_roja == Decimal("6.0")
    assert umbrales.icap_roja == Decimal("10.5")
    assert umbrales.gat_inconsistencia_pp == Decimal("1.5")


def test_every_threshold_field_is_mapped() -> None:
    """Un campo nuevo sin mapear se quedaría en su default en silencio."""
    from domain.models import UmbralesBanderas

    umbrales = umbrales_desde_config()
    assert set(umbrales.model_dump()) == set(UmbralesBanderas.model_fields)


# ─── Contexto de mercado ──────────────────────────────────────


@pytest.mark.requires_docker
@pytest.mark.usefixtures("real_db")
class TestContexto:
    async def test_context_comes_from_the_banxico_series(self) -> None:
        from api.dependencies import get_contexto
        from cli.seed import run_seed
        from core.db import get_sessionmaker

        await run_seed()

        async with get_sessionmaker()() as session:
            contexto = await get_contexto(session)

        assert isinstance(contexto, ContextoMercado)
        # UDI real del seed (2026-07-26, la más reciente de la serie).
        assert contexto.valor_udi == Decimal("8.791887")
        # INPC de junio 2026 contra junio 2025.
        assert Decimal("3.3") < contexto.inflacion_anual < Decimal("3.4")
        assert contexto.params_fiscales.tasa_retencion_capital == Decimal("0.9000")

    async def test_context_falls_back_when_the_series_are_empty(self) -> None:
        """Sin ingesta de Banxico, se sirve con los valores de respaldo."""
        from api.dependencies import get_contexto
        from cli.seed import run_seed
        from core.db import get_sessionmaker
        from core.settings import settings
        from domain.orm import ValorSerieEconomica

        await run_seed()
        async with get_sessionmaker()() as session:
            await session.execute(ValorSerieEconomica.__table__.delete())
            await session.commit()

        async with get_sessionmaker()() as session:
            contexto = await get_contexto(session)

        assert contexto.valor_udi == settings.udi_valor_fallback
        assert contexto.inflacion_anual == settings.inflacion_anual_fallback

    async def test_a_udi_published_ahead_of_time_is_not_used_yet(self) -> None:
        """Banxico publica la UDI con diez días de adelanto.

        Tomar el máximo de la serie haría que los límites de cobertura en pesos
        se calcularan con un valor que aún no rige. Con el seed no se notaba —el
        CSV no trae fechas futuras—; con la ingesta de la fase 7 sí.
        """
        from datetime import date, timedelta

        from sqlalchemy import select

        from api.dependencies import CLAVE_SERIE_UDI, get_contexto
        from cli.seed import run_seed
        from core.db import get_sessionmaker
        from domain.orm import SerieEconomica, ValorSerieEconomica

        await run_seed()
        async with get_sessionmaker()() as session:
            serie_id = await session.scalar(
                select(SerieEconomica.id).where(SerieEconomica.clave_banxico == CLAVE_SERIE_UDI)
            )
            session.add(
                ValorSerieEconomica(
                    serie_id=serie_id,
                    fecha=date.today() + timedelta(days=10),
                    valor=Decimal("99.999999"),
                )
            )
            await session.commit()

        async with get_sessionmaker()() as session:
            contexto = await get_contexto(session)

        assert contexto.valor_udi != Decimal("99.999999")

    async def test_missing_fiscal_parameters_fail_loudly(self) -> None:
        """Calcular sin ISR daría números optimistas: mejor 503 visible."""
        from fastapi import HTTPException

        from api.dependencies import get_contexto
        from core.db import get_sessionmaker

        async with get_sessionmaker()() as session:
            with pytest.raises(HTTPException) as exc:
                await get_contexto(session)

        assert exc.value.status_code == 503
        assert "cli seed" in exc.value.detail
