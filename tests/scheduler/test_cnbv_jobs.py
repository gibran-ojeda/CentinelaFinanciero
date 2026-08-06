"""Tests de los dos jobs de la fase 8: los boletines y la frescura."""

from __future__ import annotations

import shutil
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import desc, select

from core.db import session_scope
from domain.enums import EstadoJob, EstadoTasa, FuenteTasa
from domain.orm import IndicadorFinanciero, Institucion, JobRun, Producto, Tasa
from ingest_cnbv import fuentes
from ingest_cnbv.downloader import Publicacion
from scheduler.jobs.cnbv import JOB_ID, JOB_ID_FRESCURA, cnbv_boletines, frescura_check

pytestmark = pytest.mark.requires_docker

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "cnbv"

ARCHIVOS: dict[str, tuple[str, int, int]] = {
    fuentes.BOLETIN_BANCA.clave: ("banca_202605.xlsx", 2026, 5),
    fuentes.BOLETIN_SOFIPO.clave: ("sofipos_202603.xlsx", 2026, 3),
    fuentes.NCYAT_SOFIPO.clave: ("nicap_sofipos_202605.pdf", 2026, 5),
}


class DescargadorFalso:
    """Sirve los fixtures como si fueran lo último publicado."""

    def __init__(self, roto: str | None = None) -> None:
        self.roto = roto

    def _fuente_de(self, sector: str, tema: str) -> fuentes.Fuente:
        return next(f for f in fuentes.FUENTES if (f.sector, f.tema) == (sector, tema))

    async def ultimo(self, *, sector: str, tema: str, extension: str | None = None) -> Publicacion:
        fuente = self._fuente_de(sector, tema)
        archivo, anio, mes = ARCHIVOS[fuente.clave]
        return Publicacion(
            sector=sector,
            tema=tema,
            subtema="",
            archivo=archivo,
            ruta=f"/PortafolioInformacion/{archivo}",
            bytes=1,
            anio=anio,
            mes=mes,
        )

    async def descargar(self, publicacion: Publicacion, destino: Path) -> Path:
        destino.parent.mkdir(parents=True, exist_ok=True)
        if self.roto and publicacion.archivo == self.roto:
            # Un libro OOXML válido pero sin las hojas que se esperan: es lo
            # que se vería si la CNBV rehiciera el boletín.
            from openpyxl import Workbook

            libro = Workbook()
            libro.save(destino)
            return destino
        shutil.copyfile(FIXTURES / publicacion.archivo, destino)
        return destino

    async def cerrar(self) -> None:
        return None


@pytest.fixture
def descargador(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> DescargadorFalso:
    doble = DescargadorFalso()
    _instalar(monkeypatch, tmp_path, doble)
    return doble


def _instalar(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, doble: DescargadorFalso) -> None:
    from ingest_cnbv import loader

    original = loader.cargar

    async def cargar_con_doble(**kwargs: Any) -> Any:
        kwargs.setdefault("descargador", doble)
        kwargs.setdefault("directorio", tmp_path)
        return await original(**kwargs)

    monkeypatch.setattr(loader, "cargar", cargar_con_doble)
    monkeypatch.setattr("scheduler.jobs.cnbv.loader.cargar", cargar_con_doble)


async def _ultima(job_id: str) -> JobRun:
    """La corrida más reciente, desempatada por `id`.

    Por `id` y no por `inicio`: ese campo lo pone `now()` de Postgres, que
    devuelve el instante en que **empezó la transacción** — dos corridas
    seguidas pueden empatar, y entonces `LIMIT 1` elige una arbitraria. En un
    test que corre el job dos veces para comprobar que la segunda se omite,
    empatar significa leer el estado de la primera. `id` es la secuencia:
    monótona y única. Mismo criterio que `cli/revisiones.py`.
    """
    async with session_scope() as session:
        fila = await session.scalar(
            select(JobRun).where(JobRun.job_id == job_id).order_by(desc(JobRun.id)).limit(1)
        )
    assert fila is not None
    return fila


# ─── Ingesta de boletines ─────────────────────────────────────


async def test_a_run_with_new_data_succeeds_and_records_it(
    catalogo_cargado: None, descargador: DescargadorFalso
) -> None:
    await cnbv_boletines()

    corrida = await _ultima(JOB_ID)
    assert corrida.estado is EstadoJob.EXITOSO
    metricas = dict(corrida.metricas or {})
    assert metricas["fuentes"][fuentes.BOLETIN_BANCA.clave]["creados"] > 0
    assert metricas["banderas"]["instituciones"] > 0


async def test_a_day_without_new_periods_is_skipped(
    catalogo_cargado: None, descargador: DescargadorFalso
) -> None:
    """Lo normal: la CNBV publica con rezago y el job corre todos los días."""
    await cnbv_boletines()
    await cnbv_boletines()

    corrida = await _ultima(JOB_ID)
    assert corrida.estado is EstadoJob.OMITIDO
    assert "periodos nuevos" in (corrida.metricas or {})["motivo_omision"]


async def test_the_hot_kill_switch_stops_it(
    catalogo_cargado: None, descargador: DescargadorFalso
) -> None:
    from core.config_store import effective, set_value

    await set_value("cnbv_ingesta_enabled", "false", motivo="prueba", actor="test")
    await effective.refresh()
    try:
        await cnbv_boletines()
    finally:
        await set_value("cnbv_ingesta_enabled", "true", motivo="fin", actor="test")
        await effective.refresh()

    corrida = await _ultima(JOB_ID)
    assert corrida.estado is EstadoJob.OMITIDO
    async with session_scope() as session:
        # El seed siembra indicadores de las dos instituciones ilustrativas;
        # lo que no puede haber es ninguno de una real.
        real = await session.scalar(
            select(IndicadorFinanciero.id)
            .join(Institucion, Institucion.id == IndicadorFinanciero.institucion_id)
            .where(Institucion.es_demostracion.is_(False))
            .limit(1)
        )
    assert real is None


async def test_a_format_change_fails_the_run_but_loads_the_rest(
    catalogo_cargado: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§8: nunca se cargan datos malinterpretados, y se dice cuál se rompió."""
    _instalar(monkeypatch, tmp_path, DescargadorFalso(roto="banca_202605.xlsx"))

    with pytest.raises(RuntimeError, match="formato"):
        await cnbv_boletines()

    corrida = await _ultima(JOB_ID)
    assert corrida.estado is EstadoJob.FALLIDO

    # El de SOFIPOs sí entró: que la CNBV rehaga el de banca no puede dejar
    # sin datos a las sociedades.
    async with session_scope() as session:
        sofipo = await session.scalar(
            select(IndicadorFinanciero.id)
            .join(Institucion, Institucion.id == IndicadorFinanciero.institucion_id)
            .where(Institucion.nombre == "Finsus")
            .limit(1)
        )
    assert sofipo is not None


def test_both_jobs_are_registered() -> None:
    from scheduler.registry import build_registry

    registro = {j.id: j for j in build_registry()}
    assert registro[JOB_ID].enabled is True
    assert registro[JOB_ID].lock_ttl_seconds == 1800
    assert registro[JOB_ID_FRESCURA].enabled is True


# ─── Frescura ─────────────────────────────────────────────────


async def test_freshness_reports_every_source(catalogo_cargado: None) -> None:
    await frescura_check()

    corrida = await _ultima(JOB_ID_FRESCURA)
    assert corrida.estado is EstadoJob.EXITOSO
    fuentes_medidas = (corrida.metricas or {})["fuentes"]
    assert set(fuentes_medidas) == {f.value for f in FuenteTasa if f is not FuenteTasa.AGREGADOR}


async def test_a_stale_source_is_flagged_without_failing_the_run(
    catalogo_cargado: None,
) -> None:
    """El job mira y lo dice. Que un dato esté viejo no es un fallo suyo."""
    async with session_scope() as session:
        producto = await session.scalar(select(Producto).limit(1))
        assert producto is not None
        session.add(
            Tasa(
                producto_id=producto.id,
                tasa_nominal=Decimal("6.0"),
                # Muy por encima del SLA de dos días de Banxico.
                fecha_dato=date.today() - timedelta(days=90),
                fuente=FuenteTasa.BANXICO_API,
                estado=EstadoTasa.VIGENTE,
            )
        )

    await frescura_check()

    corrida = await _ultima(JOB_ID_FRESCURA)
    assert corrida.estado is EstadoJob.EXITOSO
    metricas = dict(corrida.metricas or {})
    assert FuenteTasa.BANXICO_API.value in metricas["fuera_de_sla"]
    assert metricas["todo_dentro_de_sla"] is False


async def test_a_source_without_data_is_not_stale(catalogo_cargado: None) -> None:
    """Sin datos no está obsoleta: es que todavía no se usa."""
    await frescura_check()

    metricas = dict((await _ultima(JOB_ID_FRESCURA)).metricas or {})
    llm = metricas["fuentes"][FuenteTasa.LLM_RESEARCH.value]
    assert llm["ultima"] is None
    assert llm["dentro_de_sla"] is True


async def test_an_enabled_watched_source_with_zero_rows_is_reported(
    catalogo_cargado: None,
) -> None:
    """Lo que el endpoint no puede saber, este job sí.

    El endpoint trata «sin datos» como sano porque no sabe qué jobs están
    encendidos. Con el gate encendido y cero observaciones, la fuente no es
    «aún no se usa»: es un job que dice estar vivo y no ha entregado nada.
    Las informativas (MANUAL, LLM) nunca entran aquí.
    """
    await frescura_check()

    metricas = dict((await _ultima(JOB_ID_FRESCURA)).metricas or {})
    # El seed no trae filas BANXICO_API ni FETCH_DIRIGIDO, y los gates
    # arrancan encendidos por default.
    assert FuenteTasa.BANXICO_API.value in metricas["vigiladas_sin_datos"]
    assert FuenteTasa.FETCH_DIRIGIDO.value in metricas["vigiladas_sin_datos"]
    assert FuenteTasa.MANUAL.value not in metricas["vigiladas_sin_datos"]
    # Y no hace fallar la corrida: mirar y decirlo es el trabajo.
    assert (await _ultima(JOB_ID_FRESCURA)).estado is EstadoJob.EXITOSO


async def test_the_cnbv_is_measured_against_its_indicators(
    catalogo_cargado: None, descargador: DescargadorFalso
) -> None:
    """La CNBV no publica tasas: medirla contra `tasas` la dejaría sin datos."""
    await cnbv_boletines()
    await frescura_check()

    metricas = dict((await _ultima(JOB_ID_FRESCURA)).metricas or {})
    cnbv = metricas["fuentes"][FuenteTasa.CNBV.value]
    assert cnbv["ultima"] == "2026-05-31"
    assert cnbv["observaciones"] > 0
