"""Tests de la carga: de los boletines reales a las banderas del comparador.

Contra Postgres real y con los fixtures reales de la CNBV. El descargador se
sustituye por un doble que devuelve los ficheros de `tests/fixtures/cnbv/` — lo
que se prueba aquí es la carga, no la red, y ésa ya tiene sus propios tests.
"""

from __future__ import annotations

import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from core.db import session_scope
from domain.enums import NivelCapitalizacion, Severidad, TipoBandera
from domain.orm import Bandera, IndicadorFinanciero, Institucion
from ingest_cnbv import fuentes
from ingest_cnbv.downloader import BoletinNoPublicado, Publicacion
from ingest_cnbv.loader import cargar

pytestmark = pytest.mark.requires_docker

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "cnbv"

#: Qué fixture responde a cada fuente, y con qué periodo lo publica la CNBV.
#: Banca es mensual y SOFIPOs trimestral: de ahí que los periodos no coincidan.
ARCHIVOS: dict[str, tuple[str, int, int]] = {
    fuentes.BOLETIN_BANCA.clave: ("banca_202605.xlsx", 2026, 5),
    fuentes.BOLETIN_SOFIPO.clave: ("sofipos_202603.xlsx", 2026, 3),
    fuentes.NCYAT_SOFIPO.clave: ("nicap_sofipos_202605.pdf", 2026, 5),
}


class DescargadorFalso:
    """Sirve los fixtures como si fueran lo último que publicó la CNBV."""

    def __init__(self, ausentes: set[str] | None = None) -> None:
        self.ausentes = ausentes or set()
        self.descargas: list[str] = []

    def _fuente_de(self, sector: str, tema: str) -> fuentes.Fuente:
        for fuente in fuentes.FUENTES:
            if (fuente.sector, fuente.tema) == (sector, tema):
                return fuente
        raise AssertionError(f"fuente no declarada: {sector} / {tema}")

    async def ultimo(self, *, sector: str, tema: str, extension: str | None = None) -> Publicacion:
        fuente = self._fuente_de(sector, tema)
        if fuente.clave in self.ausentes:
            raise BoletinNoPublicado(f"nada publicado de {sector} / {tema}")
        archivo, anio, mes = ARCHIVOS[fuente.clave]
        return Publicacion(
            sector=sector,
            tema=tema,
            subtema="",
            archivo=archivo,
            ruta=f"/PortafolioInformacion/{archivo}",
            bytes=FIXTURES.joinpath(archivo).stat().st_size,
            anio=anio,
            mes=mes,
        )

    async def descargar(self, publicacion: Publicacion, destino: Path) -> Path:
        self.descargas.append(publicacion.archivo)
        destino.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURES / publicacion.archivo, destino)
        return destino

    async def cerrar(self) -> None:
        return None


async def _indicadores_de(nombre: str) -> list[IndicadorFinanciero]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(IndicadorFinanciero)
                    .join(Institucion, Institucion.id == IndicadorFinanciero.institucion_id)
                    .where(Institucion.nombre == nombre)
                    .order_by(IndicadorFinanciero.periodo)
                )
            )
            .scalars()
            .all()
        )


async def _correr(tmp_path: Path, **extra: object) -> object:
    return await cargar(
        descargador=DescargadorFalso(),  # type: ignore[arg-type]
        directorio=tmp_path,
        **extra,  # type: ignore[arg-type]
    )


# ─── Carga ────────────────────────────────────────────────────


async def test_the_bulletins_load_into_indicators(catalogo_cargado: None, tmp_path: Path) -> None:
    await _correr(tmp_path)

    # Ualá es banco múltiple: su periodo es el del boletín mensual.
    uala = await _indicadores_de("Ualá")
    assert [i.periodo for i in uala] == [date(2026, 5, 31)]
    assert uala[0].imor is not None and uala[0].imor > Decimal("10")
    assert uala[0].icap is not None


async def test_each_source_keeps_its_own_period(catalogo_cargado: None, tmp_path: Path) -> None:
    """Banca es mensual, el boletín de SOFIPOs trimestral y el NICAP mensual.

    Una SOFIPO acaba con **dos filas**: la de marzo con su morosidad y la de
    mayo con su nivel de capitalización. No se funden: cada cifra es de cuando
    es, y §15 obliga a enseñarlo.
    """
    await _correr(tmp_path)

    finsus = await _indicadores_de("Finsus")
    assert [i.periodo for i in finsus] == [date(2026, 3, 31), date(2026, 5, 31)]
    assert finsus[0].imor is not None and finsus[0].nicap_nivel is None
    assert finsus[1].imor is None and finsus[1].nicap_nivel is not None

    hey = await _indicadores_de("Hey Banco")
    assert [i.periodo for i in hey] == [date(2026, 5, 31)]


async def test_the_nicap_pdf_fills_a_column_no_xlsx_has(
    catalogo_cargado: None, tmp_path: Path
) -> None:
    """El NICAP sólo se publica en PDF: sin él, ninguna SOFIPO tendría nivel."""
    await _correr(tmp_path)

    kubo = await _indicadores_de("kubo.financiero")
    nivel = next(i.nicap_nivel for i in kubo if i.nicap_nivel is not None)
    assert nivel is NivelCapitalizacion.N1


async def test_a_nicap_under_review_stays_empty(catalogo_cargado: None, tmp_path: Path) -> None:
    """Crediclub sale `n.d.`: la CNBV lo está revisando, no está en N4."""
    await _correr(tmp_path)

    crediclub = await _indicadores_de("Crediclub")
    assert all(i.nicap_nivel is None for i in crediclub)


async def test_an_institution_reported_under_another_figure_still_loads(
    catalogo_cargado: None, tmp_path: Path
) -> None:
    """Nu México está en el catálogo como banco y la CNBV lo publica en SOFIPOs.

    Filtrar por categoría lo dejaría sin datos por una discrepancia de
    clasificación que no le toca resolver a la ingesta.
    """
    await _correr(tmp_path)

    nu = await _indicadores_de("Nu México")
    assert nu and nu[0].imor == Decimal("5.7400")


async def test_the_source_url_is_stored(catalogo_cargado: None, tmp_path: Path) -> None:
    """Sin el enlace al boletín, un indicador no es auditable."""
    await _correr(tmp_path)

    uala = await _indicadores_de("Ualá")
    assert uala[0].fuente_url is not None
    assert uala[0].fuente_url.startswith("https://portafolioinfo.cnbv.gob.mx/")


async def test_two_sources_writing_the_same_period_do_not_erase_each_other(
    catalogo_cargado: None, tmp_path: Path
) -> None:
    """El IMOR de una SOFIPO viene del XLSX y su NICAP de un PDF distinto."""
    await _correr(tmp_path)

    klar = await _indicadores_de("Klar")
    con_imor = [i for i in klar if i.imor is not None]
    con_nivel = [i for i in klar if i.nicap_nivel is not None]
    assert con_imor and con_nivel


# ─── Idempotencia y omisión ───────────────────────────────────


async def test_a_period_already_loaded_is_not_downloaded_again(
    catalogo_cargado: None, tmp_path: Path
) -> None:
    """La CNBV publica con rezago: el job corre a diario y casi nunca hay nada."""
    await _correr(tmp_path)

    doble = DescargadorFalso()
    segunda = await cargar(descargador=doble, directorio=tmp_path)  # type: ignore[arg-type]

    assert doble.descargas == []
    assert all(f.omitida for f in segunda.fuentes)
    assert segunda.hubo_cambios is False


async def test_forcing_reloads_without_duplicating(catalogo_cargado: None, tmp_path: Path) -> None:
    await _correr(tmp_path)
    segunda = await _correr(tmp_path, forzar=True)

    assert segunda.hubo_cambios  # type: ignore[attr-defined]
    uala = await _indicadores_de("Ualá")
    assert len(uala) == 1  # se actualizó, no se duplicó


async def test_an_unpublished_source_is_skipped_not_failed(
    catalogo_cargado: None, tmp_path: Path
) -> None:
    doble = DescargadorFalso(ausentes={fuentes.NCYAT_SOFIPO.clave})

    reporte = await cargar(descargador=doble, directorio=tmp_path)  # type: ignore[arg-type]

    ncyat = next(f for f in reporte.fuentes if f.clave == fuentes.NCYAT_SOFIPO.clave)
    assert ncyat.omitida is not None
    assert reporte.hubo_errores is False


# ─── El encadenamiento con las banderas ───────────────────────


async def test_a_high_imor_raises_a_red_flag_without_anyone_touching_it(
    catalogo_cargado: None, tmp_path: Path
) -> None:
    """Criterio de aceptación de la fase.

    Libertad reporta un IMOR de 44.37% en marzo de 2026, muy por encima del
    umbral rojo de 6%. Cargar el boletín tiene que bastar para que salga
    marcada en el comparador.
    """
    await _correr(tmp_path)

    async with session_scope() as session:
        banderas = (
            (
                await session.execute(
                    select(Bandera)
                    .join(Institucion, Institucion.id == Bandera.institucion_id)
                    .where(
                        Institucion.nombre == "Libertad Servicios Financieros",
                        Bandera.activa.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )

    imor = [b for b in banderas if b.tipo is TipoBandera.IMOR]
    assert imor, "el IMOR de 44.37% tenía que levantar bandera"
    assert imor[0].severidad is Severidad.ROJA
    # §11: la bandera dice de qué periodo es el dato que la originó.
    assert imor[0].periodo_dato == date(2026, 3, 31)


async def test_nothing_new_means_no_recompute(catalogo_cargado: None, tmp_path: Path) -> None:
    await _correr(tmp_path)
    segunda = await _correr(tmp_path)

    assert segunda.banderas == {}  # type: ignore[attr-defined]
