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
    assert await _contar(Producto) == 48
    assert await _contar(ParametroFiscal) == 2
    assert await _contar(SerieEconomica) == 2
    assert await _contar(ValorSerieEconomica) == 21
    assert await _contar(FuenteTasas) == 18


async def test_klar_carries_the_terms_it_actually_offers() -> None:
    """Los plazos sembrados eran el calendario de CETES con el nombre de Klar.

    Klar publica 7, 30, 90, 180 y 365 días; el catálogo tenía 28, 91, 182 y
    364, que es exactamente lo que la regla 2 del extractor prohíbe hacer. Y
    sus tasas —8.20 a 8.50— eran las de Klar Plus, no las de cualquiera.
    """
    await run_seed()

    async with session_scope() as session:
        filas = (
            (
                await session.execute(
                    select(Producto.slug, Producto.plazo_dias, Producto.activo)
                    .join(Institucion)
                    .where(Institucion.nombre == "Klar", Producto.tipo == TipoProducto.PLAZO)
                    .order_by(Producto.plazo_dias)
                )
            )
            .tuples()
            .all()
        )

    activos = {(slug, plazo) for slug, plazo, activo in filas if activo}
    assert activos == {
        ("klar-fija-7", 7),
        ("klar-fija-30", 30),
        ("klar-fija-90", 90),
        ("klar-fija-180", 180),
        ("klar-fija-365", 365),
    }
    # Los inventados siguen ahí, apagados: su historial de tasas no se borra.
    apagados = {slug for slug, _, activo in filas if not activo}
    assert apagados == {"klar-plazo-28", "klar-plazo-91", "klar-plazo-182", "klar-plazo-364"}


async def test_the_two_sight_products_of_klar_declare_their_published_name() -> None:
    """Sin el nombre, «Cuenta» e «Inversión Flexible» son indistinguibles.

    Las dos son VISTA y sin plazo, así que caen en la misma casilla y el
    pipeline las manda a hueco — que es lo correcto mientras nada las separe.
    """
    await run_seed()

    async with session_scope() as session:
        filas = (
            (
                await session.execute(
                    select(Producto.slug, Producto.nombre_publicado)
                    .join(Institucion)
                    .where(
                        Institucion.nombre == "Klar",
                        Producto.tipo == TipoProducto.VISTA,
                        Producto.activo.is_(True),
                    )
                    .order_by(Producto.slug)
                )
            )
            .tuples()
            .all()
        )

    assert filas == [("klar-flexible", "Inversión Flexible"), ("klar-vista", "Cuenta")]


async def test_hey_carries_its_pagares_and_no_sight_account() -> None:
    """Hey publica a 7 y 28 días y nada a la vista.

    Su `hey-vista` sembrado al 7.00 % era la tasa de Fan Hey / Hey Pro **a 7
    días** colocada en una cuenta que no existe.
    """
    await run_seed()

    async with session_scope() as session:
        filas = (
            (
                await session.execute(
                    select(Producto.slug, Producto.plazo_dias, Producto.activo)
                    .join(Institucion)
                    .where(Institucion.nombre == "Hey Banco")
                    .order_by(Producto.slug)
                )
            )
            .tuples()
            .all()
        )

    assert filas == [
        ("hey-plazo-28", 28, True),
        ("hey-plazo-7", 7, True),
        ("hey-vista", None, False),
    ]


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


async def test_a_source_dropped_from_the_yaml_is_switched_off() -> None:
    """Corregir una URL en el YAML dejaba viva la vieja.

    La clave del upsert incluye la URL, así que cambiarla **inserta otra fila**
    y la muerta se queda activa: la corrida seguiría pidiéndola cada cuatro
    horas para siempre. Se apaga, no se borra — la fila conserva su historial y
    `cli fuentes list` la sigue nombrando.
    """
    await run_seed()
    async with session_scope() as session:
        institucion = await session.scalar(select(Institucion).where(Institucion.slug == "klar"))
        assert institucion is not None
        session.add(
            FuenteTasas(
                institucion_id=institucion.id,
                url="https://www.klar.mx/url-que-ya-no-esta-en-el-yaml",
                nivel=2,
            )
        )

    await run_seed()

    async with session_scope() as session:
        huerfana = await session.scalar(
            select(FuenteTasas).where(
                FuenteTasas.url == "https://www.klar.mx/url-que-ya-no-esta-en-el-yaml"
            )
        )
        viva = await session.scalar(
            select(FuenteTasas).where(FuenteTasas.url == "https://www.klar.mx/inversion")
        )
    assert huerfana is not None  # sigue ahí, con su historial
    assert huerfana.activa is False
    assert huerfana.pausada_motivo is not None
    assert viva is not None and viva.activa is True


async def test_seeding_does_not_resurrect_a_source_that_switched_itself_off() -> None:
    """`desplegar.sh` corre el seed en cada push.

    Si impusiera `activa: true` por defecto, la autopausa duraría hasta el
    siguiente deploy y no serviría de nada: una fuente que se apagó por
    acumular fallos volvería a intentarse seis veces al día hasta apagarse otra
    vez. El YAML declara la intención; que esté encendida hoy es estado del
    runtime, y reanudarla sigue siendo un acto humano.
    """
    await run_seed()
    async with session_scope() as session:
        fuente = await session.scalar(
            select(FuenteTasas).where(FuenteTasas.url == "https://www.klar.mx/inversion")
        )
        assert fuente is not None
        fuente.activa = False
        fuente.pausada_motivo = "6 fallos seguidos: HTTP 500"

    await run_seed()

    async with session_scope() as session:
        despues = await session.scalar(
            select(FuenteTasas).where(FuenteTasas.url == "https://www.klar.mx/inversion")
        )
    assert despues is not None
    assert despues.activa is False
    assert despues.pausada_motivo is not None


async def test_the_yaml_still_wins_when_it_says_so() -> None:
    """La otra mitad: `activa: false` en el YAML sí se impone siempre.

    Es lo que mantiene apagadas a Openbank —403 al bot— y a Supertasas, aunque
    alguien las reanude a mano sin arreglar el motivo.
    """
    await run_seed()
    async with session_scope() as session:
        openbank = await session.scalar(
            select(FuenteTasas).where(
                FuenteTasas.url == "https://www.openbank.mx/cuenta-debito-open-plus"
            )
        )
        assert openbank is not None
        openbank.activa = True

    await run_seed()

    async with session_scope() as session:
        despues = await session.scalar(
            select(FuenteTasas).where(
                FuenteTasas.url == "https://www.openbank.mx/cuenta-debito-open-plus"
            )
        )
    assert despues is not None
    assert despues.activa is False


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
