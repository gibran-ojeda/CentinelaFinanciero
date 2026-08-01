"""Tests de la corrida del nivel 3: a quién se investiga y qué se hace después."""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from core.db import session_scope
from domain.enums import EstadoTasa, FuenteTasa
from domain.orm import FuenteTasas, Institucion, Producto, RevisionTasa, Tasa
from llm.providers.base import LlamadaHerramienta, RespuestaLLM
from rates_agent import investigacion
from rates_agent.search import Resultado, SearchExecutor

pytestmark = pytest.mark.requires_docker

HOY = date(2026, 8, 5)


class MotorFalso:
    nombre = "falso"

    def __init__(self, urls: list[str]) -> None:
        self._urls = urls

    async def buscar(self, consulta: str, *, maximo: int) -> list[Resultado]:
        return [Resultado(titulo="t", url=u, resumen="r", motor=self.nombre) for u in self._urls]


class ClienteFalso:
    """Contesta siempre lo mismo: una búsqueda y luego el hallazgo."""

    def __init__(self, url: str, tasa: str = "12.5", plazo: int = 90) -> None:
        self._url = url
        self._tasa = tasa
        self._plazo = plazo
        self._turno = 0

    async def completar(self, **kwargs: Any) -> RespuestaLLM:
        self._turno += 1
        base = dict(
            modelo="deepseek-v4-flash",
            tokens_entrada=100,
            tokens_salida=50,
            costo_usd=0.0001,
            latencia_ms=10,
        )
        if kwargs.get("herramientas") and self._turno == 1:
            return RespuestaLLM(
                contenido="",
                herramientas=(
                    LlamadaHerramienta(
                        id="c1",
                        nombre="web_search",
                        argumentos={"consulta": "tasas"},
                        argumentos_crudos='{"consulta": "tasas"}',
                    ),
                ),
                **base,  # type: ignore[arg-type]
            )
        return RespuestaLLM(
            contenido=json.dumps(
                {
                    "hallazgos": [
                        {
                            "producto": "Inversión a plazo",
                            "tipo": "PLAZO",
                            "plazo_dias": self._plazo,
                            "tasa_nominal": self._tasa,
                            "url": self._url,
                            "confianza": "alta",
                        }
                    ],
                    "sin_datos": False,
                }
            ),
            **base,  # type: ignore[arg-type]
        )

    async def cerrar(self) -> None:
        return None


#: El catálogo semilla no tiene ni una tasa VIGENTE: todas son `AGREGADOR` en
#: `PENDIENTE_REVISION`, porque nadie las ha verificado contra la fuente. Así
#: que los tests que necesitan una lectura previa se la crean.
async def _vigente(nombre: str, plazo: int, *, dias_atras: int, tasa: str = "8.0") -> Producto:
    producto = await _producto_de(nombre, plazo)
    async with session_scope() as session:
        session.add(
            Tasa(
                producto_id=producto.id,
                tasa_nominal=Decimal(tasa),
                fecha_dato=HOY - timedelta(days=dias_atras),
                fuente=FuenteTasa.FETCH_DIRIGIDO,
                fuente_url="https://previa.test/",
                estado=EstadoTasa.VIGENTE,
            )
        )
    return producto


async def _todas_frescas() -> None:
    """Una VIGENTE de hoy y una fuente activa para cada institución."""
    async with session_scope() as session:
        instituciones = (await session.execute(select(Institucion))).scalars().all()
        for institucion in instituciones:
            session.add(
                FuenteTasas(institucion_id=institucion.id, url=f"https://x.test/{institucion.id}")
            )
        productos = (await session.execute(select(Producto))).scalars().all()
        for producto in productos:
            session.add(
                Tasa(
                    producto_id=producto.id,
                    tasa_nominal=Decimal("8.0"),
                    fecha_dato=HOY,
                    fuente=FuenteTasa.FETCH_DIRIGIDO,
                    estado=EstadoTasa.VIGENTE,
                )
            )


async def _producto_de(nombre: str, plazo: int) -> Producto:
    async with session_scope() as session:
        producto = await session.scalar(
            select(Producto)
            .join(Institucion, Institucion.id == Producto.institucion_id)
            .where(Institucion.nombre == nombre, Producto.plazo_dias == plazo)
        )
    assert producto is not None, f"{nombre} no tiene producto a {plazo} días"
    return producto


# ─── A quién se investiga ─────────────────────────────────────


async def test_a_fresh_institution_is_not_investigated(catalogo_cargado: None) -> None:
    """Si el fetch del lunes trajo la tasa, el miércoles no se busca."""
    await _todas_frescas()

    candidatas = await investigacion._candidatas(HOY)

    assert candidatas == []


async def test_a_stale_institution_is_a_candidate(catalogo_cargado: None) -> None:
    """Su tasa pasó del SLA: o el fetch falla en silencio, o cambió la página."""
    await _todas_frescas()
    await _vigente("Finsus", 91, dias_atras=60)
    async with session_scope() as session:
        producto = await _producto_de("Finsus", 91)
        for tasa in (
            (
                await session.execute(
                    select(Tasa).where(Tasa.producto_id == producto.id, Tasa.fecha_dato == HOY)
                )
            )
            .scalars()
            .all()
        ):
            await session.delete(tasa)

    candidatas = await investigacion._candidatas(HOY)

    # El resto de Finsus sigue fresco, así que la institución no es candidata
    # por eso — hace falta que **toda** su lectura sea vieja.
    assert "Finsus" not in {c.nombre for c in candidatas}


async def test_an_institution_whose_whole_reading_is_old_is_a_candidate(
    catalogo_cargado: None,
) -> None:
    await _todas_frescas()
    async with session_scope() as session:
        finsus = await session.scalar(select(Institucion).where(Institucion.nombre == "Finsus"))
        assert finsus is not None
        productos = (
            (await session.execute(select(Producto).where(Producto.institucion_id == finsus.id)))
            .scalars()
            .all()
        )
        for tasa in (
            (
                await session.execute(
                    select(Tasa).where(Tasa.producto_id.in_([p.id for p in productos]))
                )
            )
            .scalars()
            .all()
        ):
            tasa.fecha_dato = HOY - timedelta(days=60)

    candidatas = await investigacion._candidatas(HOY)

    finsus = next(c for c in candidatas if c.nombre == "Finsus")
    assert "SLA" in finsus.motivo


async def test_an_institution_without_any_rate_is_a_candidate(
    catalogo_cargado: None,
) -> None:
    """Las tasas de Finsus en el semilla son del agregador y sin verificar.

    Ninguna está VIGENTE, así que desde el punto de vista del nivel 3 esa
    institución no tiene lectura y es candidata por el motivo más fuerte.
    """
    candidatas = await investigacion._candidatas(HOY)

    finsus = next(c for c in candidatas if c.nombre == "Finsus")
    assert finsus.motivo == "sin ninguna tasa vigente"
    assert len(candidatas) > 5


async def test_an_institution_with_only_a_level3_source_is_a_candidate(
    catalogo_cargado: None,
) -> None:
    """La portada de nivel 3 no cuenta como fuente del fetch dirigido.

    Se le da a CAME una tasa vigente fresca para aislar el motivo: sin el
    filtro de nivel, su portada contaba como «con fuente» y una institución
    que el extractor jamás va a leer quedaba fuera del researcher.
    """
    async with session_scope() as session:
        producto_id = await session.scalar(
            select(Producto.id).where(Producto.slug == "came-plazo-364")
        )
        assert producto_id is not None
        session.add(
            Tasa(
                producto_id=producto_id,
                tasa_nominal=Decimal("9.00"),
                fecha_dato=HOY,
                fuente=FuenteTasa.MANUAL,
                estado=EstadoTasa.VIGENTE,
            )
        )

    candidatas = await investigacion._candidatas(HOY)

    came = next(c for c in candidatas if c.nombre == "CAME")
    assert came.motivo == "sin fuente activa para el fetch dirigido"


# ─── Qué se hace con lo que sale ──────────────────────────────


async def _solo_finsus_stale() -> Producto:
    """Todo fresco menos Finsus. Así la corrida investiga exactamente a una."""
    await _todas_frescas()
    producto = await _producto_de("Finsus", 91)
    async with session_scope() as session:
        finsus = await session.scalar(select(Institucion).where(Institucion.nombre == "Finsus"))
        assert finsus is not None
        suyos = (
            (await session.execute(select(Producto).where(Producto.institucion_id == finsus.id)))
            .scalars()
            .all()
        )
        filas = (
            (
                await session.execute(
                    select(Tasa).where(Tasa.producto_id.in_([p.id for p in suyos]))
                )
            )
            .scalars()
            .all()
        )
        for fila in filas:
            fila.fecha_dato = HOY - timedelta(days=60)
    return producto


async def _correr(url: str, *, plazo: int = 91, urls_buscadas: list[str] | None = None) -> Any:
    return await investigacion.correr(
        cliente=ClienteFalso(url, plazo=plazo),  # type: ignore[arg-type]
        ejecutor=SearchExecutor(  # type: ignore[arg-type]
            [MotorFalso(urls_buscadas if urls_buscadas is not None else [url])]  # type: ignore[list-item]
        ),
        hoy=HOY,
    )


async def test_a_finding_is_written_with_its_source(catalogo_cargado: None) -> None:
    producto = await _solo_finsus_stale()

    reporte = await _correr("https://finsus.test/tasas")

    assert reporte.hallazgos == 1
    async with session_scope() as session:
        tasa = await session.scalar(
            select(Tasa).where(
                Tasa.producto_id == producto.id, Tasa.fuente == FuenteTasa.LLM_RESEARCH
            )
        )
    assert tasa is not None
    assert tasa.fuente_url == "https://finsus.test/tasas"


async def test_a_research_rate_goes_to_review_when_it_moves_too_much(
    catalogo_cargado: None,
) -> None:
    """La tolerancia es de 0.5 pp y la lectura previa era 8.0%."""
    producto = await _solo_finsus_stale()

    await _correr("https://finsus.test/tasas")

    async with session_scope() as session:
        tasa = await session.scalar(
            select(Tasa).where(
                Tasa.producto_id == producto.id, Tasa.fuente == FuenteTasa.LLM_RESEARCH
            )
        )
        assert tasa is not None
        revision = await session.scalar(
            select(RevisionTasa).where(RevisionTasa.tasa_id == tasa.id)
        )
    assert tasa.estado is EstadoTasa.PENDIENTE_REVISION
    assert revision is not None


async def test_an_invented_url_never_reaches_the_database(catalogo_cargado: None) -> None:
    """La invariante, extremo a extremo: si nadie vio la URL, no hay tasa."""
    producto = await _solo_finsus_stale()

    reporte = await _correr(
        "https://finsus.test/inventada", urls_buscadas=["https://finsus.test/real"]
    )

    assert reporte.hallazgos == 0
    assert reporte.descartados_por_url == 1
    async with session_scope() as session:
        tasa = await session.scalar(
            select(Tasa).where(
                Tasa.producto_id == producto.id, Tasa.fuente == FuenteTasa.LLM_RESEARCH
            )
        )
    assert tasa is None


async def test_a_tenor_the_catalogue_does_not_have_is_a_structured_gap(
    catalogo_cargado: None,
) -> None:
    """Encajar 360 días en el producto de 364 es el error que trajo el agregador.

    Y un hueco en texto libre dentro de `errores` era invisible para
    `cli revisiones list`: viaja con el mismo shape que los del nivel 2.
    """
    await _solo_finsus_stale()

    reporte = await _correr("https://finsus.test/tasas", plazo=360)

    assert reporte.hallazgos == 1
    assert reporte.publicadas == 0 and reporte.en_revision == 0
    assert reporte.errores == []
    assert len(reporte.huecos_catalogo) == 1
    hueco = reporte.huecos_catalogo[0]
    assert hueco["institucion"] == "Finsus"
    assert hueco["plazo_dias"] == 360
    assert hueco["url"] == "https://finsus.test/tasas"
    assert reporte.como_metricas()["huecos_catalogo"] == reporte.huecos_catalogo


async def test_all_engines_down_marks_the_run_degraded(catalogo_cargado: None) -> None:
    """Sin buscador no hay URLs permitidas, así que no puede publicarse nada."""
    await _solo_finsus_stale()

    reporte = await _correr("https://finsus.test/tasas", urls_buscadas=[])

    assert reporte.degradada is True
    assert reporte.publicadas == 0


async def test_the_run_reports_its_cost(catalogo_cargado: None) -> None:
    await _solo_finsus_stale()

    reporte = await _correr("https://finsus.test/tasas")

    assert reporte.candidatas == 1
    assert reporte.investigadas == 1
    assert reporte.tokens > 0
    assert reporte.costo_usd == pytest.approx(0.0002)
    assert reporte.busquedas == 1


# ─── El job ───────────────────────────────────────────────────


def test_the_job_is_registered_but_starts_off() -> None:
    """El nivel 3 es el camino más caro: se enciende a mano, cuando toque."""
    from scheduler.jobs.research import JOB_ID
    from scheduler.registry import build_registry

    spec = next(j for j in build_registry() if j.id == JOB_ID)
    assert spec.enabled is False
    assert spec.lock_ttl_seconds == 3600


async def test_the_job_skips_when_nothing_is_stale(
    catalogo_cargado: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lo bueno: significa que el nivel 2 cubre todo el catálogo."""
    from sqlalchemy import desc

    from domain.enums import EstadoJob
    from domain.orm import JobRun
    from scheduler.jobs import research

    await _todas_frescas()
    # El job arma su propio `ClienteLLM`: sin este doble, un fallo de selección
    # de candidatas se traduce en llamadas de verdad a la API.
    monkeypatch.setattr(
        research.investigacion,
        "correr",
        lambda **_: _sin_candidatas(),
    )

    await research.tasas_research_abierta()

    async with session_scope() as session:
        corrida = await session.scalar(
            select(JobRun)
            .where(JobRun.job_id == research.JOB_ID)
            .order_by(desc(JobRun.inicio))
            .limit(1)
        )
    assert corrida is not None
    assert corrida.estado is EstadoJob.OMITIDO
    assert "stale" in (corrida.metricas or {})["motivo_omision"]


async def _sin_candidatas() -> investigacion.ReporteInvestigacion:
    return investigacion.ReporteInvestigacion()


async def test_the_job_hot_kill_switch_stops_it(
    catalogo_cargado: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import desc

    from core.config_store import effective, set_value
    from domain.enums import EstadoJob
    from domain.orm import JobRun
    from scheduler.jobs import research

    llamado = False

    async def _no_deberia(**_: Any) -> investigacion.ReporteInvestigacion:
        nonlocal llamado
        llamado = True
        return investigacion.ReporteInvestigacion()

    monkeypatch.setattr(research.investigacion, "correr", _no_deberia)
    await set_value("tasas_research_enabled", "false", motivo="prueba", actor="test")
    await effective.refresh()
    try:
        await research.tasas_research_abierta()
    finally:
        await set_value("tasas_research_enabled", "true", motivo="fin", actor="test")
        await effective.refresh()

    assert llamado is False
    async with session_scope() as session:
        corrida = await session.scalar(
            select(JobRun)
            .where(JobRun.job_id == research.JOB_ID)
            .order_by(desc(JobRun.inicio))
            .limit(1)
        )
    assert corrida is not None
    assert corrida.estado is EstadoJob.OMITIDO
