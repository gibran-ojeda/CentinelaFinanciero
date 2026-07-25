"""Tests del esquema ORM contra un Postgres real.

Se usa Postgres y no SQLite porque lo que se está verificando son constraints
—checks, uniques, cascadas— que SQLite aplica de forma distinta o no aplica.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from domain.enums import (
    CategoriaInstitucion,
    EstadoTasa,
    FuenteTasa,
    Liquidez,
    TipoInstrumento,
    TipoProducto,
    TipoSeguro,
)
from domain.orm import Base, Institucion, Producto, Tasa

pytestmark = pytest.mark.requires_docker


@pytest.fixture(scope="session")
def postgres_url() -> AsyncIterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16") as container:
        yield (
            f"postgresql+asyncpg://{container.username}:{container.password}"
            f"@{container.get_container_host_ip()}:{container.get_exposed_port(5432)}"
            f"/{container.dbname}"
        )


@pytest.fixture
async def session(postgres_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(postgres_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _institucion(nombre: str = "FinSUS") -> Institucion:
    return Institucion(
        nombre=nombre,
        slug=nombre.lower().replace(" ", "-"),
        categoria=CategoriaInstitucion.SOFIPO,
        tipo_seguro=TipoSeguro.PROSOFIPO,
    )


def _producto(institucion: Institucion, **kwargs: object) -> Producto:
    defaults: dict[str, object] = {
        "institucion": institucion,
        "nombre": "Ahorro a plazo",
        "slug": "finsus-ahorro-plazo-91",
        "tipo": TipoProducto.PLAZO,
        "instrumento": TipoInstrumento.DEPOSITO_SOFIPO,
        "plazo_dias": 91,
        "monto_minimo": Decimal("100.00"),
        "liquidez": Liquidez.AL_VENCIMIENTO,
    }
    defaults.update(kwargs)
    return Producto(**defaults)  # type: ignore[arg-type]


async def test_schema_creates_every_table(session: AsyncSession) -> None:
    esperadas = {
        "instituciones",
        "productos",
        "tasas",
        "indicadores_financieros",
        "banderas",
        "series_economicas",
        "valores_serie",
        "parametros_fiscales",
        "fuentes_tasas",
        "revisiones_tasas",
        "config_store",
        "config_versions",
        "job_runs",
    }
    assert esperadas <= set(Base.metadata.tables)


async def test_institution_name_is_unique(session: AsyncSession) -> None:
    session.add(_institucion())
    await session.commit()
    session.add(
        Institucion(
            nombre="FinSUS",
            slug="finsus-2",
            categoria=CategoriaInstitucion.SOFIPO,
            tipo_seguro=TipoSeguro.PROSOFIPO,
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_invalid_enum_value_is_rejected(session: AsyncSession) -> None:
    """El check de enum evita que una ingesta escriba una categoría inventada."""
    session.add(
        Institucion(
            nombre="Rara",
            slug="rara",
            categoria="COOPERATIVA",  # type: ignore[arg-type]
            tipo_seguro=TipoSeguro.PROSOFIPO,
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_vista_product_cannot_have_a_term(session: AsyncSession) -> None:
    """Un VISTA con plazo no se sabría clasificar en el filtro de §7."""
    session.add(
        _producto(
            _institucion(),
            tipo=TipoProducto.VISTA,
            plazo_dias=28,
            slug="finsus-vista-mal",
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_term_product_requires_a_term(session: AsyncSession) -> None:
    session.add(
        _producto(_institucion(), tipo=TipoProducto.PLAZO, plazo_dias=None, slug="mal-plazo")
    )
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_vista_product_without_term_is_valid(session: AsyncSession) -> None:
    session.add(
        _producto(
            _institucion(),
            tipo=TipoProducto.VISTA,
            plazo_dias=None,
            liquidez=Liquidez.INMEDIATA,
            slug="finsus-vista",
        )
    )
    await session.commit()


async def test_same_rate_observation_cannot_be_loaded_twice(session: AsyncSession) -> None:
    """Reimportar el mismo CSV no debe duplicar observaciones."""
    producto = _producto(_institucion())
    session.add(producto)
    await session.commit()

    for _ in range(2):
        session.add(
            Tasa(
                producto=producto,
                tasa_nominal=Decimal("9.5000"),
                fecha_dato=date(2026, 7, 25),
                fuente=FuenteTasa.MANUAL,
                estado=EstadoTasa.VIGENTE,
            )
        )
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_negative_rate_is_rejected(session: AsyncSession) -> None:
    producto = _producto(_institucion())
    session.add(producto)
    await session.commit()
    session.add(
        Tasa(
            producto=producto,
            tasa_nominal=Decimal("-1.0"),
            fecha_dato=date(2026, 7, 25),
            fuente=FuenteTasa.MANUAL,
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_rates_are_stored_as_decimal_not_float(session: AsyncSession) -> None:
    """Ningún importe ni tasa puede volver de la base como float."""
    producto = _producto(_institucion())
    session.add(producto)
    await session.commit()
    session.add(
        Tasa(
            producto=producto,
            tasa_nominal=Decimal("9.1234"),
            gat_nominal=Decimal("9.5678"),
            fecha_dato=date(2026, 7, 25),
            fuente=FuenteTasa.MANUAL,
        )
    )
    await session.commit()

    recuperada = (await session.get(Producto, producto.id)) is not None
    assert recuperada
    tasa = (await session.execute(Tasa.__table__.select())).mappings().one()
    assert isinstance(tasa["tasa_nominal"], Decimal)
    assert tasa["tasa_nominal"] == Decimal("9.1234")
    assert isinstance(tasa["gat_nominal"], Decimal)


async def test_deleting_an_institution_cascades(session: AsyncSession) -> None:
    institucion = _institucion()
    producto = _producto(institucion)
    session.add(producto)
    await session.commit()

    await session.delete(institucion)
    await session.commit()

    restantes = (await session.execute(Producto.__table__.select())).all()
    assert restantes == []
