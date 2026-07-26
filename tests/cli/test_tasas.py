"""Tests del alta manual de tasas.

Dos propiedades que importan: append-only (una tasa nunca se edita, se
supersede) y validación defensiva (un dato imposible se rechaza en vez de
publicarse, porque esto es un comparador financiero).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from cli.seed import DEFAULT_SEEDS_DIR, run_seed
from cli.tasas import ImportError_, import_csv
from core.db import session_scope
from domain.enums import EstadoTasa, FuenteTasa
from domain.orm import Producto, Tasa

pytestmark = [pytest.mark.requires_docker, pytest.mark.usefixtures("real_db")]

CABECERA = (
    "producto_slug,tasa_nominal,gat_nominal,gat_real,fecha_dato,fuente,fuente_url,estado,notas"
)


def _csv(tmp_path: Path, *filas: str) -> Path:
    ruta = tmp_path / "tasas.csv"
    ruta.write_text("\n".join([CABECERA, *filas]) + "\n", encoding="utf-8")
    return ruta


async def _contar_tasas() -> int:
    async with session_scope() as session:
        total = await session.scalar(select(func.count()).select_from(Tasa))
    return int(total or 0)


async def test_imports_the_full_seed_dataset() -> None:
    await run_seed()
    report = await import_csv(DEFAULT_SEEDS_DIR / "tasas.csv")

    assert report.creadas == 37
    assert report.errores == []
    assert await _contar_tasas() == 37


async def test_seed_dataset_publishes_only_verified_rates() -> None:
    """La regla del catálogo: sólo lo verificado en fuente primaria va VIGENTE."""
    await run_seed()
    report = await import_csv(DEFAULT_SEEDS_DIR / "tasas.csv")

    # Siete VIGENTE: las cinco gubernamentales verificadas contra el SIE de
    # Banxico y cetesdirecto, más las dos de instituciones ficticias — que no
    # tienen fuente que verificar porque no existen, y van marcadas con ◆.
    assert report.por_estado["VIGENTE"] == 7
    assert report.por_estado["PENDIENTE_REVISION"] == 30

    async with session_scope() as session:
        vigentes = (
            (
                await session.execute(
                    select(Producto.slug)
                    .join(Tasa)
                    .where(Tasa.estado == EstadoTasa.VIGENTE)
                    .order_by(Producto.slug)
                )
            )
            .scalars()
            .all()
        )

    assert set(vigentes) == {
        "bonddia",
        "cetes-182",
        "cetes-28",
        "cetes-364",
        "cetes-91",
        "ahorra-mas-plazo-364",
        "alcancia-plazo-182",
    }


async def test_reimporting_the_same_file_creates_nothing() -> None:
    """La clave natural (producto, fecha, fuente) hace la carga repetible."""
    await run_seed()
    await import_csv(DEFAULT_SEEDS_DIR / "tasas.csv")

    segundo = await import_csv(DEFAULT_SEEDS_DIR / "tasas.csv")

    assert segundo.creadas == 0
    assert segundo.duplicadas == 37
    assert await _contar_tasas() == 37


async def test_a_new_observation_supersedes_without_deleting(tmp_path: Path) -> None:
    """Append-only: la tasa anterior sigue ahí, sólo deja de ser la vigente."""
    await run_seed()
    await import_csv(_csv(tmp_path, "cetes-28,6.29,,,2026-07-16,MANUAL,,VIGENTE,"))
    await import_csv(_csv(tmp_path, "cetes-28,6.18,,,2026-07-23,MANUAL,,VIGENTE,"))

    async with session_scope() as session:
        observaciones = (
            (
                await session.execute(
                    select(Tasa)
                    .join(Producto)
                    .where(Producto.slug == "cetes-28")
                    .order_by(Tasa.fecha_dato)
                )
            )
            .scalars()
            .all()
        )

    assert len(observaciones) == 2
    assert [o.tasa_nominal for o in observaciones] == [Decimal("6.2900"), Decimal("6.1800")]


async def test_provenance_is_preserved(tmp_path: Path) -> None:
    """§19: toda tasa publicada conserva fuente_url y fecha_dato."""
    await run_seed()
    await import_csv(
        _csv(
            tmp_path,
            "cetes-28,6.18,,,2026-07-23,MANUAL,https://banxico.org.mx/x,VIGENTE,de la subasta",
        )
    )

    async with session_scope() as session:
        tasa = await session.scalar(select(Tasa))

    assert tasa is not None
    assert tasa.fuente_url == "https://banxico.org.mx/x"
    assert tasa.fecha_dato == date(2026, 7, 23)
    assert tasa.fuente is FuenteTasa.MANUAL
    assert tasa.notas == "de la subasta"


async def test_implausible_rate_is_rejected(tmp_path: Path) -> None:
    """Un 950 en vez de 9.50 no puede acabar publicado."""
    await run_seed()
    report = await import_csv(_csv(tmp_path, "cetes-28,950,,,2026-07-23,MANUAL,,VIGENTE,"))

    assert report.creadas == 0
    assert len(report.errores) == 1
    assert "fuera de rango plausible" in report.errores[0]
    assert await _contar_tasas() == 0


async def test_negative_rate_is_rejected(tmp_path: Path) -> None:
    await run_seed()
    report = await import_csv(_csv(tmp_path, "cetes-28,-1,,,2026-07-23,MANUAL,,VIGENTE,"))
    assert report.creadas == 0
    assert await _contar_tasas() == 0


async def test_future_dated_observation_is_rejected(tmp_path: Path) -> None:
    """Una tasa no puede observarse antes de existir."""
    await run_seed()
    manana = (date.today() + timedelta(days=30)).isoformat()
    report = await import_csv(_csv(tmp_path, f"cetes-28,6.18,,,{manana},MANUAL,,VIGENTE,"))

    assert report.creadas == 0
    assert "futuro" in report.errores[0]


async def test_unknown_product_is_reported_without_stopping_the_rest(
    tmp_path: Path,
) -> None:
    await run_seed()
    report = await import_csv(
        _csv(
            tmp_path,
            "producto-que-no-existe,7.0,,,2026-07-23,MANUAL,,VIGENTE,",
            "cetes-28,6.18,,,2026-07-23,MANUAL,,VIGENTE,",
        )
    )

    assert report.creadas == 1
    assert len(report.errores) == 1
    assert "producto desconocido" in report.errores[0]


async def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    await run_seed()
    report = await import_csv(
        _csv(tmp_path, "cetes-28,6.18,,,2026-07-23,MANUAL,,VIGENTE,"), dry_run=True
    )

    assert report.creadas == 1
    assert await _contar_tasas() == 0


async def test_missing_columns_fail_loudly(tmp_path: Path) -> None:
    ruta = tmp_path / "malo.csv"
    ruta.write_text("producto_slug,tasa_nominal\ncetes-28,6.18\n", encoding="utf-8")

    with pytest.raises(ImportError_, match="fecha_dato"):
        await import_csv(ruta)


async def test_missing_file_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(ImportError_, match="no existe"):
        await import_csv(tmp_path / "no-existe.csv")


async def test_defaults_to_manual_and_current(tmp_path: Path) -> None:
    """Sin columnas `fuente` ni `estado`, se asume alta manual vigente."""
    await run_seed()
    ruta = tmp_path / "minimo.csv"
    ruta.write_text(
        "producto_slug,tasa_nominal,fecha_dato\ncetes-28,6.18,2026-07-23\n", encoding="utf-8"
    )
    await import_csv(ruta)

    async with session_scope() as session:
        tasa = await session.scalar(select(Tasa))

    assert tasa is not None
    assert tasa.fuente is FuenteTasa.MANUAL
    assert tasa.estado is EstadoTasa.VIGENTE
