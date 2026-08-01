"""Tests de la carga de catálogos.

El criterio central es la idempotencia: `cli seed` corre en cada deploy, así
que la segunda ejecución no puede duplicar nada. Se prueba contra Postgres real
porque lo que se verifica son claves naturales y constraints.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from cli.seed import DEFAULT_SEEDS_DIR, SeedError, run_seed
from core.db import session_scope
from domain.enums import CategoriaInstitucion, TipoProducto, TipoSeguro
from domain.orm import (
    FuenteTasas,
    Institucion,
    ParametroFiscal,
    Producto,
    SerieEconomica,
    ValorSerieEconomica,
)

pytestmark = [pytest.mark.requires_docker, pytest.mark.usefixtures("real_db")]


async def _contar(modelo: type) -> int:
    async with session_scope() as session:
        total = await session.scalar(select(func.count()).select_from(modelo))
    return int(total or 0)


async def test_seed_loads_the_whole_mvp_catalog() -> None:
    report = await run_seed()

    assert report.total_creados > 0
    assert await _contar(Institucion) == 17
    assert await _contar(Producto) == 40
    assert await _contar(ParametroFiscal) == 2
    assert await _contar(SerieEconomica) == 2
    assert await _contar(ValorSerieEconomica) == 21
    assert await _contar(FuenteTasas) == 18


async def test_no_institution_is_marked_as_demo() -> None:
    """Las ficticias salieron del producto; el catálogo entero es real.

    La columna `es_demostracion` sigue existiendo como invariante —una
    institución marcada jamás se sirve— pero desde la purga nada la enciende.
    Este test fija que el seed no la reintroduzca.
    """
    await run_seed()

    async with session_scope() as session:
        demo = (
            (
                await session.execute(
                    select(Institucion.nombre).where(Institucion.es_demostracion.is_(True))
                )
            )
            .scalars()
            .all()
        )

    assert demo == []


async def test_seed_is_idempotent() -> None:
    """Correrlo dos veces deja la base igual que correrlo una."""
    await run_seed()
    conteos = {
        modelo.__name__: await _contar(modelo)
        for modelo in (Institucion, Producto, ParametroFiscal, SerieEconomica, FuenteTasas)
    }

    segundo = await run_seed()

    assert segundo.total_creados == 0
    for modelo in (Institucion, Producto, ParametroFiscal, SerieEconomica, FuenteTasas):
        assert await _contar(modelo) == conteos[modelo.__name__]


async def test_second_run_reports_no_changes() -> None:
    await run_seed()
    segundo = await run_seed()
    assert segundo.total_actualizados == 0
    assert segundo.sin_cambios["instituciones"] == 17


async def test_seed_updates_changed_fields_without_duplicating(tmp_path: Path) -> None:
    """Editar la semilla actualiza la fila, no crea otra."""
    await run_seed()

    (tmp_path / "instituciones.yaml").write_text(
        "instituciones:\n"
        "  - nombre: Finsus\n"
        "    slug: finsus\n"
        "    categoria: SOFIPO\n"
        "    tipo_seguro: PROSOFIPO\n"
        "    url_sitio: https://nueva-url.example\n",
        encoding="utf-8",
    )
    report = await run_seed(tmp_path)

    assert report.creados.get("instituciones", 0) == 0
    assert report.actualizados["instituciones"] == 1
    assert await _contar(Institucion) == 17

    async with session_scope() as session:
        finsus = await session.scalar(select(Institucion).where(Institucion.nombre == "Finsus"))
    assert finsus is not None
    assert finsus.url_sitio == "https://nueva-url.example"


async def test_regulatory_figure_drives_insurance_coverage() -> None:
    """Nu debe quedar como banco con IPAB, no como SOFIPO."""
    await run_seed()

    async with session_scope() as session:
        nu = await session.scalar(select(Institucion).where(Institucion.nombre == "Nu México"))
        mercado_pago = await session.scalar(
            select(Institucion).where(Institucion.nombre == "Mercado Pago")
        )
        finsus = await session.scalar(select(Institucion).where(Institucion.nombre == "Finsus"))

    assert nu is not None and nu.categoria is CategoriaInstitucion.BANCO_DIGITAL
    assert nu.tipo_seguro is TipoSeguro.IPAB
    assert mercado_pago is not None and mercado_pago.tipo_seguro is TipoSeguro.NINGUNO
    assert finsus is not None and finsus.tipo_seguro is TipoSeguro.PROSOFIPO


async def test_sight_products_have_no_term_and_term_products_do() -> None:
    await run_seed()

    async with session_scope() as session:
        productos = (await session.execute(select(Producto))).scalars().all()

    assert productos
    for producto in productos:
        if producto.tipo is TipoProducto.VISTA:
            assert producto.plazo_dias is None, producto.slug
        else:
            assert producto.plazo_dias and producto.plazo_dias > 0, producto.slug


async def test_current_fiscal_year_uses_the_2026_withholding_rate() -> None:
    """0.90% del artículo 24 de la LIF 2026, no el 0.50% de 2025."""
    await run_seed()

    async with session_scope() as session:
        param = await session.scalar(select(ParametroFiscal).where(ParametroFiscal.anio == 2026))
        anterior = await session.scalar(
            select(ParametroFiscal).where(ParametroFiscal.anio == 2025)
        )

    assert param is not None and param.tasa_retencion_capital == Decimal("0.9000")
    assert anterior is not None and anterior.tasa_retencion_capital == Decimal("0.5000")


async def test_udi_and_inpc_series_are_seeded() -> None:
    """Cubre la brecha: la fase 4 los necesita antes de la ingesta de Banxico."""
    await run_seed()

    async with session_scope() as session:
        claves = set((await session.execute(select(SerieEconomica.clave_banxico))).scalars())
        udi = await session.scalar(
            select(ValorSerieEconomica.valor)
            .join(SerieEconomica)
            .where(SerieEconomica.clave_banxico == "SP68257")
            .order_by(ValorSerieEconomica.fecha.desc())
            .limit(1)
        )

    assert claves == {"SP68257", "SP1"}
    assert udi is not None and Decimal("8") < udi < Decimal("10")


async def test_the_seed_writes_no_financial_indicators() -> None:
    """La regla que protege al producto de fabricar información financiera.

    Inventar IMOR, ICAP o NICAP para una institución real sería fabricar
    información financiera sobre una empresa concreta. Desde la purga de las
    ficticias no queda ningún caso legítimo: el único escritor de
    `indicadores_financieros` es la ingesta de la CNBV, que los lee de los
    boletines. Este test fija que el seed no vuelva a escribir ahí.
    """
    from domain.orm import IndicadorFinanciero

    await run_seed()

    async with session_scope() as session:
        total = await session.scalar(select(func.count()).select_from(IndicadorFinanciero))

    assert int(total or 0) == 0


async def test_unknown_institution_reference_fails_loudly(tmp_path: Path) -> None:
    """Un producto huérfano debe reventar la carga, no colarse a medias."""
    (tmp_path / "instituciones.yaml").write_text("instituciones: []\n", encoding="utf-8")
    (tmp_path / "productos.yaml").write_text(
        "productos:\n"
        "  - institucion: Banco Inexistente\n"
        "    nombre: Producto\n"
        "    slug: producto\n"
        "    tipo: VISTA\n"
        "    instrumento: DEPOSITO_BANCARIO\n"
        "    liquidez: INMEDIATA\n",
        encoding="utf-8",
    )

    with pytest.raises(SeedError, match="Banco Inexistente"):
        await run_seed(tmp_path)

    # La transacción no dejó nada a medias.
    assert await _contar(Producto) == 0


async def test_missing_seed_files_are_tolerated(tmp_path: Path) -> None:
    report = await run_seed(tmp_path)
    assert report.total_creados == 0


async def test_a_missing_seeds_directory_is_not_tolerated(tmp_path: Path) -> None:
    """Un archivo suelto que falte se tolera; el directorio entero, no.

    Es la diferencia entre una semilla incompleta y una carga que no hace
    nada. Sin esto, `cli seed` en un contenedor sin `seeds/` anunciaba éxito
    con la base vacía — que fue exactamente lo que pasó.
    """
    with pytest.raises(SeedError, match="directorio de semillas"):
        await run_seed(tmp_path / "no-existe")


def test_default_seeds_directory_exists() -> None:
    assert (DEFAULT_SEEDS_DIR / "instituciones.yaml").exists()


def test_the_seeds_directory_is_resolved_from_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cómo se encuentran los catálogos cuando el paquete está instalado.

    En la imagen, `cli` vive en `site-packages` y subir dos niveles desde el
    módulo apunta a `/usr/local/lib/python3.12/seeds`, que no existe. Lo que
    manda es el directorio de trabajo, que en el contenedor es `/app`.
    """
    from cli.seed import _default_seeds_dir

    monkeypatch.delenv("SEEDS_DIR", raising=False)

    (tmp_path / "seeds").mkdir()
    monkeypatch.chdir(tmp_path)
    assert _default_seeds_dir() == tmp_path / "seeds"

    # Y `SEEDS_DIR` gana sobre todo lo demás.
    monkeypatch.setenv("SEEDS_DIR", str(tmp_path / "otro"))
    assert _default_seeds_dir() == tmp_path / "otro"
