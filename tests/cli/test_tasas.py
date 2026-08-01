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
from cli.tasas import ImportError_, import_csv, listar_pendientes, retirar_sustituidas
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

    assert report.creadas == 35
    assert report.errores == []
    assert await _contar_tasas() == 35


async def test_importing_invalidates_the_comparador_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sin esto el sitio queda en blanco cinco minutos después de cada deploy.

    El script de despliegue espera a que la API esté sana y **luego** siembra.
    En ese hueco, el healthcheck de `web` pide la portada cada 30 s, la API la
    calcula con la tabla vacía y la cachea con el TTL del ConfigStore. Un alta
    que no invalida deja servida esa respuesta vacía todo el TTL — y hace
    fallar el gate de la portada en un despliegue que estaba bien.
    """
    await run_seed()
    llamadas = 0

    async def _espia() -> int:
        nonlocal llamadas
        llamadas += 1
        return 0

    monkeypatch.setattr("cli.tasas.cache.invalidar", _espia)

    await import_csv(_csv(tmp_path, "cetes-28,6.18,,,2026-07-23,MANUAL,,VIGENTE,"))
    assert llamadas == 1

    # Se invalida aunque no se cree nada: la respuesta vacía cacheada durante el
    # despliegue sigue ahí, y una reimportación idempotente es justo lo que hace
    # el despliegue en cada corrida a partir de la primera.
    await import_csv(_csv(tmp_path, "cetes-28,6.18,,,2026-07-23,MANUAL,,VIGENTE,"))
    assert llamadas == 2


async def test_a_dry_run_does_not_touch_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Una simulación no escribe, así que no hay nada que invalidar."""
    await run_seed()
    llamadas = 0

    async def _espia() -> int:
        nonlocal llamadas
        llamadas += 1
        return 0

    monkeypatch.setattr("cli.tasas.cache.invalidar", _espia)

    await import_csv(_csv(tmp_path, "cetes-28,6.18,,,2026-07-23,MANUAL,,VIGENTE,"), dry_run=True)
    assert llamadas == 0


async def test_seed_dataset_publishes_only_verified_rates() -> None:
    """La regla del catálogo: sólo lo verificado en fuente primaria va VIGENTE."""
    await run_seed()
    report = await import_csv(DEFAULT_SEEDS_DIR / "tasas.csv")

    # Cinco VIGENTE: las gubernamentales verificadas contra el SIE de Banxico
    # y cetesdirecto. Todo lo demás es de agregador y queda pendiente.
    assert report.por_estado["VIGENTE"] == 5
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
    }


async def test_reimporting_the_same_file_creates_nothing() -> None:
    """La clave natural (producto, fecha, fuente) hace la carga repetible."""
    await run_seed()
    await import_csv(DEFAULT_SEEDS_DIR / "tasas.csv")

    segundo = await import_csv(DEFAULT_SEEDS_DIR / "tasas.csv")

    assert segundo.creadas == 0
    assert segundo.duplicadas == 35
    assert await _contar_tasas() == 35


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


# ─── Lista de revisión ────────────────────────────────────────


async def test_the_review_list_names_what_cannot_be_published() -> None:
    """Lo que el sitio público no puede mostrar, y por qué en cada caso.

    Dos motivos distintos caen en la misma lista: una tasa que existe pero no
    se confirmó contra la institución, y un producto sin ninguna tasa. Los dos
    son invisibles con `mostrar_tasas_sin_verificar=false`, así que los dos
    son trabajo de la misma sesión de revisión.
    """
    await run_seed()
    await import_csv(DEFAULT_SEEDS_DIR / "tasas.csv")

    lista = await listar_pendientes()
    motivos = {p.motivo for p in lista.pendientes}

    assert motivos == {"sin verificar", "sin tasa"}
    assert sum(p.motivo == "sin verificar" for p in lista.pendientes) == 30

    # Ninguna de las cinco VIGENTE aparece: CETES y BONDDIA salen de fuente
    # primaria.
    slugs = {p.producto_slug for p in lista.pendientes}
    assert "cetes-28" not in slugs
    assert "bonddia" not in slugs


async def test_the_review_list_carries_the_official_url_to_open() -> None:
    """La URL que hay que abrir es la curada, no la de la tasa anterior.

    `fuente_url` dice de dónde salió el dato la última vez —hoy, de un
    agregador— y eso es justamente lo que se va a corregir. Lo que sirve para
    verificar es la página de la propia institución.
    """
    await run_seed()
    await import_csv(DEFAULT_SEEDS_DIR / "tasas.csv")

    lista = await listar_pendientes()
    urls = lista.urls_oficiales["Finsus"]

    assert [u for u, _ in urls] == ["https://www.finsus.mx/inversion"]
    # La marca de JavaScript viaja: esa página no se lee con un cliente HTTP
    # plano y quien revisa tiene que abrirla en el navegador.
    assert urls[0][1] is True

    rendido = lista.render()
    assert "https://www.finsus.mx/inversion" in rendido
    assert "requiere JS" in rendido


async def test_an_empty_review_list_says_so() -> None:
    await run_seed()
    lista = await listar_pendientes()
    lista.pendientes.clear()

    assert "Nada pendiente" in lista.render()


# ─── La invariante del agregador ──────────────────────────────


async def test_an_aggregator_rate_cannot_be_current(tmp_path: Path) -> None:
    """Lo que recopiló un tercero no se publica, y el alta lo impide.

    Se hace valer al escribir y no filtrando al leer: así no depende de que
    cada consulta futura se acuerde de excluirlo.
    """
    await run_seed()
    ruta = _csv(
        tmp_path,
        "cetes-28,6.18,,,2026-07-23,AGREGADOR,https://ejemplo.mx/,VIGENTE,",
    )

    report = await import_csv(ruta)

    assert report.creadas == 0
    assert len(report.errores) == 1
    assert "AGREGADOR no puede estar VIGENTE" in report.errores[0]
    assert await _contar_tasas() == 0


async def test_an_aggregator_rate_is_accepted_as_pending(tmp_path: Path) -> None:
    """Pendiente sí: es el contraste contra el que se medirá la lectura oficial."""
    await run_seed()
    ruta = _csv(
        tmp_path,
        "cetes-28,6.18,,,2026-07-23,AGREGADOR,https://ejemplo.mx/,PENDIENTE_REVISION,",
    )

    report = await import_csv(ruta)

    assert report.creadas == 1
    assert report.errores == []


async def test_the_seed_holds_no_aggregator_rate_as_current() -> None:
    """El catálogo semilla cumple la invariante: 30 de agregador, ninguna vigente."""
    await run_seed()
    await import_csv(DEFAULT_SEEDS_DIR / "tasas.csv")

    async with session_scope() as session:
        agregadas = (
            (await session.execute(select(Tasa).where(Tasa.fuente == FuenteTasa.AGREGADOR)))
            .scalars()
            .all()
        )

    assert len(agregadas) == 30
    assert all(t.estado is EstadoTasa.PENDIENTE_REVISION for t in agregadas)


# ─── Retiro de filas de agregador sustituidas ─────────────────


async def _con_hey_sustituida(tmp_path: Path) -> Path:
    """Seed + import + lectura oficial VIGENTE de hey-vista; copia del CSV."""
    await run_seed()
    await import_csv(DEFAULT_SEEDS_DIR / "tasas.csv")
    async with session_scope() as session:
        producto = await session.scalar(select(Producto).where(Producto.slug == "hey-vista"))
        assert producto is not None
        session.add(
            Tasa(
                producto_id=producto.id,
                tasa_nominal=Decimal("7.10"),
                fecha_dato=date.today(),
                fuente=FuenteTasa.FETCH_DIRIGIDO,
                estado=EstadoTasa.VIGENTE,
            )
        )

    copia = tmp_path / "tasas.csv"
    copia.write_text(
        (DEFAULT_SEEDS_DIR / "tasas.csv").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return copia


async def test_a_superseded_aggregator_row_gets_commented_out(tmp_path: Path) -> None:
    """La promesa del encabezado del CSV, hecha comando.

    Retirar es comentar: la línea original queda a la vista con la razón, y
    todo lo demás se conserva byte a byte.
    """
    copia = await _con_hey_sustituida(tmp_path)

    reporte = await retirar_sustituidas(copia)

    assert [r[0] for r in reporte.retiradas] == ["hey-vista"]
    assert reporte.conservadas == 29

    original = (DEFAULT_SEEDS_DIR / "tasas.csv").read_text(encoding="utf-8").splitlines()
    nuevo = copia.read_text(encoding="utf-8").splitlines()
    distintas = [i for i, (a, b) in enumerate(zip(original, nuevo, strict=True)) if a != b]
    assert len(distintas) == 1
    assert nuevo[distintas[0]].startswith("# retirada ")
    assert "sustituida por FETCH_DIRIGIDO" in nuevo[distintas[0]]
    assert nuevo[distintas[0]].endswith(original[distintas[0]])

    # Ambos lectores la ignoran: reimportar ve una fila menos.
    segundo = await import_csv(copia)
    assert segundo.creadas == 0
    assert segundo.duplicadas == 34


async def test_retiring_is_idempotent(tmp_path: Path) -> None:
    """Una fila ya comentada deja de ser candidata: la segunda pasada no toca."""
    copia = await _con_hey_sustituida(tmp_path)
    await retirar_sustituidas(copia)
    tras_primera = copia.read_text(encoding="utf-8")

    reporte = await retirar_sustituidas(copia)

    assert reporte.retiradas == []
    assert copia.read_text(encoding="utf-8") == tras_primera


async def test_dry_run_reports_without_writing(tmp_path: Path) -> None:
    copia = await _con_hey_sustituida(tmp_path)
    antes = copia.read_text(encoding="utf-8")

    reporte = await retirar_sustituidas(copia, dry_run=True)

    assert [r[0] for r in reporte.retiradas] == ["hey-vista"]
    assert copia.read_text(encoding="utf-8") == antes
    assert "simulación" in reporte.render()


async def test_nothing_is_retired_without_an_official_reading(tmp_path: Path) -> None:
    """El contraste se queda mientras siga siendo lo único que hay."""
    await run_seed()
    await import_csv(DEFAULT_SEEDS_DIR / "tasas.csv")
    copia = tmp_path / "tasas.csv"
    copia.write_text(
        (DEFAULT_SEEDS_DIR / "tasas.csv").read_text(encoding="utf-8"), encoding="utf-8"
    )
    antes = copia.read_text(encoding="utf-8")

    reporte = await retirar_sustituidas(copia)

    assert reporte.retiradas == []
    assert reporte.conservadas == 30
    assert copia.read_text(encoding="utf-8") == antes
