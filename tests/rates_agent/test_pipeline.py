"""Tests de la corrida completa: leer, extraer, decidir.

Lo que se verifica es el orden y sus consecuencias en dinero y en resiliencia:
que una página sin cambios no cueste un token, que una fuente caída no se lleve
a las demás, y que el techo de gasto corte sin marcar la corrida como fallida.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from cli.seed import run_seed
from core.db import session_scope
from domain.enums import EstadoJob, EstadoTasa, FuenteTasa
from domain.orm import FuenteTasas, JobRun, Producto, Tasa
from llm.client import ClienteLLM
from llm.providers.base import ErrorPresupuestoAgotado, ProveedorLLM, RespuestaLLM
from rates_agent import pipeline
from rates_agent.fetcher import ErrorDescarga, Fetcher

pytestmark = [pytest.mark.requires_docker, pytest.mark.usefixtures("real_db", "real_redis")]

PAGINA = """
<html><body><article>
<h1>Inversión a plazo fijo de Finsus</h1>
<p>Estas son nuestras tasas vigentes, calculadas antes de impuestos y sujetas a
cambio sin previo aviso. El monto mínimo de inversión es de cien pesos.</p>
<table><tr><td>364 días</td><td>8.69%</td></tr></table>
</article></body></html>
"""


class TransporteFalso:
    # Representa la cadena entera, no un eslabón: estos tests van sobre el
    # pipeline y casi todas las fuentes del seed piden renderizado. Qué
    # transporte resuelve cada fuente lo prueba `test_fetcher`.
    renderiza_js = True

    def __init__(self, nombre: str, *guion: str | ErrorDescarga) -> None:
        self.nombre = nombre
        self._guion = list(guion)
        self.llamadas = 0

    async def obtener(self, url: str, *, timeout_s: float) -> str:
        self.llamadas += 1
        siguiente = self._guion.pop(0) if self._guion else PAGINA
        if isinstance(siguiente, ErrorDescarga):
            raise siguiente
        return siguiente

    async def cerrar(self) -> None:
        return None


class ModeloFalso(ProveedorLLM):
    def __init__(self, *, tasas: list[dict] | None = None, sin_presupuesto: bool = False) -> None:
        self.nombre = "doble"
        self.modelo = "doble"
        self.llamadas = 0
        self._tasas = tasas if tasas is not None else []
        self._sin_presupuesto = sin_presupuesto

    async def completar(self, **kwargs: object) -> RespuestaLLM:
        self.llamadas += 1
        if self._sin_presupuesto:
            raise ErrorPresupuestoAgotado("techo diario alcanzado")
        return RespuestaLLM(
            contenido=json.dumps({"tasas": self._tasas}),
            modelo="doble",
            tokens_entrada=1000,
            tokens_salida=100,
            costo_usd=0.0002,
            latencia_ms=1,
        )

    async def ping(self) -> bool:
        return True


def _fetcher(*transportes: TransporteFalso) -> Fetcher:
    return Fetcher(
        list(transportes),  # type: ignore[arg-type]
        respetar_robots=False,
        esperas_backoff_s=(),
        espera_base_s=0.001,
        # El umbral de «página vacía» es del fetcher y lo prueba `test_fetcher`.
        # Aquí sólo hace ruido: `PAGINA` extrae 195 caracteres con trafilatura
        # 2.2 y 201 con 2.1 —la versión nueva ya no dibuja las tablas con
        # barras—, así que contra el valor de producción (200) estos tests
        # dependían de qué versión resolviera pip ese día. Y resolvió otra.
        min_caracteres=1,
    )


async def _solo_una_fuente(url: str = "https://www.finsus.mx/inversion") -> None:
    """Deja una sola fuente activa, para que la corrida sea legible."""
    await run_seed()
    async with session_scope() as session:
        fuentes = (await session.execute(select(FuenteTasas))).scalars().all()
        for fuente in fuentes:
            fuente.activa = fuente.url == url


TASA_364 = {"producto": "Plazo fijo", "tipo": "PLAZO", "plazo_dias": 364, "tasa_nominal": "8.69"}


async def test_level3_sources_are_not_fed_to_the_extractor() -> None:
    """Las portadas de nivel 3 son del researcher, no páginas de tasas.

    Dárselas al extractor paga tokens por leer marketing cada lunes y, en el
    mejor de los casos, devuelve «vacía». `nivel` es contrato, no adorno.
    """
    await run_seed()

    async with session_scope() as session:
        nivel3 = set(
            (await session.execute(select(FuenteTasas.url).where(FuenteTasas.nivel == 3)))
            .scalars()
            .all()
        )
    assert nivel3  # el seed trae las cuatro portadas de nivel 3

    urls = {fila[1] for fila in await pipeline._fuentes(None)}

    assert not urls & nivel3
    assert urls  # y las de nivel 2 siguen entrando


async def test_a_read_page_becomes_a_queued_review() -> None:
    """Primera lectura oficial: se encola, no se publica."""
    await _solo_una_fuente()
    modelo = ModeloFalso(tasas=[TASA_364])

    reporte = await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", PAGINA)), cliente=ClienteLLM(modelo)
    )

    assert reporte.fuentes == 1
    assert reporte.leidas == 1
    assert reporte.tasas_extraidas == 1
    assert reporte.en_revision == 1
    assert reporte.publicadas == 0


async def test_an_unchanged_page_costs_nothing() -> None:
    """El ahorro que hace viable correr esto cada semana."""
    await _solo_una_fuente()
    modelo = ModeloFalso(tasas=[TASA_364])

    await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", PAGINA)), cliente=ClienteLLM(modelo)
    )
    llamadas_tras_la_primera = modelo.llamadas

    reporte = await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", PAGINA)), cliente=ClienteLLM(modelo)
    )

    assert reporte.sin_cambios_en_la_pagina == 1
    assert modelo.llamadas == llamadas_tras_la_primera  # ni un token más


async def test_the_hash_is_only_stamped_after_a_successful_extraction() -> None:
    """Si la extracción revienta, la próxima corrida tiene que reintentarlo."""
    await _solo_una_fuente()

    await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", PAGINA)),
        cliente=ClienteLLM(ModeloFalso(sin_presupuesto=True)),
    )

    async with session_scope() as session:
        fuente = await session.scalar(
            select(FuenteTasas).where(FuenteTasas.url == "https://www.finsus.mx/inversion")
        )
    assert fuente is not None
    assert fuente.ultimo_hash is None


async def test_a_dead_source_does_not_cost_the_others() -> None:
    await run_seed()
    async with session_scope() as session:
        fuentes = (await session.execute(select(FuenteTasas))).scalars().all()
        activas = {"https://www.finsus.mx/inversion", "https://www.supertasas.com/"}
        for fuente in fuentes:
            fuente.activa = fuente.url in activas

    caida = ErrorDescarga("HTTP 500", transitorio=True)
    modelo = ModeloFalso(tasas=[TASA_364])
    reporte = await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", caida, caida, PAGINA, PAGINA)),
        cliente=ClienteLLM(modelo),
    )

    assert reporte.fuentes == 2
    assert reporte.fallidas == 1
    assert reporte.leidas == 1  # la otra sí se leyó


async def test_a_page_with_no_rates_is_neither_read_nor_failed() -> None:
    await _solo_una_fuente()
    vacia = "<html><body><div id='root'></div></body></html>"
    modelo = ModeloFalso()

    reporte = await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", vacia)), cliente=ClienteLLM(modelo)
    )

    assert reporte.vacias == 1
    assert reporte.fallidas == 0
    assert modelo.llamadas == 0


# ─── Salud persistente de la fuente ───────────────────────────


async def _fuente() -> FuenteTasas:
    async with session_scope() as session:
        fuente = await session.scalar(
            select(FuenteTasas).where(FuenteTasas.url == "https://www.finsus.mx/inversion")
        )
        assert fuente is not None
        session.expunge(fuente)
        return fuente


async def test_only_a_reading_with_rates_counts_as_a_success() -> None:
    """`ultima_extraccion_at` dice «se descargó»; `ultimo_exito_at`, «sirvió».

    Seis fuentes del catálogo apuntan a portadas que se descargan perfectamente
    y no publican una sola tasa. Con una única columna eran indistinguibles de
    seis lecturas buenas que no se movieron.
    """
    await _solo_una_fuente()

    reporte = await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", PAGINA)),
        cliente=ClienteLLM(ModeloFalso(tasas=[])),
    )

    assert reporte.sin_tasas == ["Finsus"]
    fuente = await _fuente()
    assert fuente.ultima_extraccion_at is not None  # sí se descargó
    assert fuente.ultimo_exito_at is None  # y no sirvió de nada


async def test_a_reading_with_rates_stamps_the_success() -> None:
    await _solo_una_fuente()

    await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", PAGINA)),
        cliente=ClienteLLM(ModeloFalso(tasas=[TASA_364])),
    )

    fuente = await _fuente()
    assert fuente.ultimo_exito_at is not None
    assert fuente.fallos_consecutivos == 0


async def test_failures_accumulate_and_a_good_read_forgets_them() -> None:
    """El contador mide **descargas**, y la memoria es corta a propósito.

    Un sitio que se cae una tarde y vuelve no tiene por qué acercarse a la
    pausa por lo que le pasó la semana pasada.
    """
    await _solo_una_fuente()
    caida = ErrorDescarga("HTTP 500", transitorio=False)

    for _ in range(2):
        await pipeline.correr(
            fetcher=_fetcher(TransporteFalso("httpx", caida)),
            cliente=ClienteLLM(ModeloFalso()),
        )
    fuente = await _fuente()
    assert fuente.fallos_consecutivos == 2
    assert fuente.ultimo_error is not None
    assert fuente.activa is True  # dos no bastan

    await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", PAGINA)),
        cliente=ClienteLLM(ModeloFalso(tasas=[TASA_364])),
    )
    fuente = await _fuente()
    assert fuente.fallos_consecutivos == 0
    assert fuente.ultimo_error is None


async def test_a_source_that_keeps_failing_pauses_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hey Banco lleva meses devolviendo «Domain Not Found» seis veces al día.

    Cada intento arrastra la cadena de transportes entera, Chromium incluido.
    Apagarla es barato; que se volviera a encender sola devolvería el problema
    a donde estaba, invisible.
    """
    await _solo_una_fuente()
    monkeypatch.setattr(pipeline.effective, "fetch_fallos_para_pausar", 2, raising=False)
    caida = ErrorDescarga("HTTP 500 Domain Not Found", transitorio=False)

    for _ in range(2):
        reporte = await pipeline.correr(
            fetcher=_fetcher(TransporteFalso("httpx", caida)),
            cliente=ClienteLLM(ModeloFalso()),
        )

    assert reporte.fuentes_pausadas == ["Finsus — https://www.finsus.mx/inversion"]
    fuente = await _fuente()
    assert fuente.activa is False
    assert fuente.pausada_motivo is not None
    assert "2 fallos seguidos" in fuente.pausada_motivo

    # Y la corrida siguiente ya no la intenta: eso es todo el ahorro.
    transporte = TransporteFalso("httpx", caida)
    reporte = await pipeline.correr(
        fetcher=_fetcher(transporte), cliente=ClienteLLM(ModeloFalso())
    )
    assert reporte.fuentes == 0
    assert transporte.llamadas == 0


async def test_an_empty_page_repeated_also_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    """El caso cetesdirecto, que hasta ahora era invisible del todo.

    Un 200 sin texto legible no es un fallo en una corrida —no abre circuito ni
    cuesta un token— pero repetido es una URL rota que nadie iba a ver, porque
    se contaba como «vacía» y ahí moría.
    """
    await _solo_una_fuente()
    monkeypatch.setattr(pipeline.effective, "fetch_fallos_para_pausar", 2, raising=False)
    vacia = "<html><body><div id='root'></div></body></html>"

    for _ in range(2):
        await pipeline.correr(
            fetcher=_fetcher(TransporteFalso("httpx", vacia)),
            cliente=ClienteLLM(ModeloFalso()),
        )

    fuente = await _fuente()
    assert fuente.activa is False
    assert fuente.pausada_motivo is not None
    assert "texto legible" in fuente.pausada_motivo


async def test_a_broken_model_does_not_pause_a_healthy_source() -> None:
    """La salud es de la fuente, no del proveedor de LLM.

    Cuando la `DEEPSEEK_API_KEY` se perdió en el despliegue, todas las
    extracciones fallaron durante días. Si eso contara como fallo de fuente,
    una llave mal puesta habría apagado el catálogo entero.
    """
    await _solo_una_fuente()

    for _ in range(6):
        await pipeline.correr(
            fetcher=_fetcher(TransporteFalso("httpx", PAGINA)),
            cliente=ClienteLLM(ModeloFalso(sin_presupuesto=True)),
        )

    fuente = await _fuente()
    assert fuente.activa is True
    assert fuente.fallos_consecutivos == 0


async def test_an_unknown_tenor_is_a_catalogue_gap() -> None:
    """360 no se encaja en el producto de 364 porque «es casi lo mismo».

    Ese redondeo es exactamente el error que traía el dato del agregador.
    """
    await _solo_una_fuente()
    modelo = ModeloFalso(
        tasas=[
            {"producto": "Plazo 360", "tipo": "PLAZO", "plazo_dias": 360, "tasa_nominal": "8.69"}
        ]
    )

    reporte = await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", PAGINA)), cliente=ClienteLLM(modelo)
    )

    assert reporte.en_revision == 0
    assert len(reporte.huecos_catalogo) == 1
    assert reporte.huecos_catalogo[0]["plazo_dias"] == 360
    assert reporte.huecos_catalogo[0]["institucion"] == "Finsus"


async def test_a_second_reading_within_tolerance_publishes_itself() -> None:
    """El caso frecuente: hay una vigente aprobada y la tasa se movió poco."""
    await _solo_una_fuente()
    async with session_scope() as session:
        producto = await session.scalar(
            select(Producto).where(Producto.slug == "finsus-plazo-364")
        )
        assert producto is not None
        session.add(
            Tasa(
                producto_id=producto.id,
                tasa_nominal=Decimal("8.50"),
                fecha_dato=date.today() - timedelta(days=7),
                fuente=FuenteTasa.FETCH_DIRIGIDO,
                estado=EstadoTasa.VIGENTE,
            )
        )

    reporte = await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", PAGINA)),
        cliente=ClienteLLM(ModeloFalso(tasas=[TASA_364])),
    )

    assert reporte.publicadas == 1
    assert reporte.en_revision == 0


async def test_the_budget_ceiling_stops_the_run_without_failing_it() -> None:
    await _solo_una_fuente()

    reporte = await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", PAGINA)),
        cliente=ClienteLLM(ModeloFalso(sin_presupuesto=True)),
    )

    assert reporte.presupuesto_agotado is True
    assert reporte.fallidas == 0


async def test_the_time_ceiling_stops_the_run_without_failing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El 2026-08-02 una corrida duró 27 minutos para 90 s de trabajo.

    Un host que no conectaba metió a la corrida en el backoff temporal y el
    pipeline es secuencial, así que trece fuentes esperaron con Chromium vivo.
    Ahora hay un tope: lo que no dé tiempo se lee dentro de cuatro horas, y eso
    no es un fallo — es lo que hace tolerable cortar.
    """
    await run_seed()
    async with session_scope() as session:
        fuentes = (await session.execute(select(FuenteTasas))).scalars().all()
        activas = {"https://www.finsus.mx/inversion", "https://www.supertasas.com/"}
        for fuente in fuentes:
            fuente.activa = fuente.url in activas

    # Arranque, primera fuente dentro del techo, y de ahí en adelante 21
    # minutos: por encima del default de 20. Se sustituye el nombre importado
    # en `pipeline`, no `time.monotonic`: parchear el módulo global se lo cambia
    # también a asyncio, que lo llama tantas veces que agota el guion antes de
    # que el bucle llegue a mirarlo.
    guion = iter([0.0, 0.0])
    monkeypatch.setattr(pipeline, "monotonic", lambda: next(guion, 21 * 60.0))

    reporte = await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", PAGINA, PAGINA)),
        cliente=ClienteLLM(ModeloFalso(tasas=[TASA_364])),
    )

    assert reporte.fuentes == 2
    assert reporte.leidas == 1  # la segunda ni se intentó
    assert reporte.cortada_por_tiempo is True
    assert reporte.fallidas == 0


def test_a_total_failure_is_distinguished_from_a_partial_one() -> None:
    """La tabla de verdad del fracaso total: todo falló, no hubo corrida."""
    assert pipeline.ReporteCorrida(fuentes=3, fallidas=3).fracaso_total is True
    assert pipeline.ReporteCorrida(fuentes=3, fallidas=2).fracaso_total is False
    assert pipeline.ReporteCorrida(fuentes=0).fracaso_total is False
    # Los dos cortes dejan fuentes sin intentar, no fallidas: ninguno dispara esto.
    assert pipeline.ReporteCorrida(fuentes=3, presupuesto_agotado=True).fracaso_total is False
    assert pipeline.ReporteCorrida(fuentes=3, cortada_por_tiempo=True).fracaso_total is False


# ─── El job ───────────────────────────────────────────────────


def test_each_pass_carries_exactly_one_transport() -> None:
    """Un transporte por pasada, no una cadena.

    Encadenarlos era el fallo silencioso: httpx contestaba primero con el
    envoltorio de la SPA, la descarga se daba por buena y Chromium no llegaba a
    abrirse. Con el reparto por `requiere_js` no hay nada que encadenar — cada
    fuente ya sabe con qué se lee. Construir el transporte no lanza Chromium
    (el arranque es perezoso), así que esto corre sin browser.
    """
    from scheduler.jobs.tasas import _armar_fetcher

    barata = [t.nombre for t in _armar_fetcher(con_navegador=False)._transportes]
    lenta = [t.nombre for t in _armar_fetcher(con_navegador=True)._transportes]

    assert barata == ["httpx"]
    assert lenta == ["navegador"]


async def test_the_job_hands_its_own_fetcher_to_the_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El job arma la cadena; el pipeline conserva la propiedad y la cierra."""
    from scheduler.jobs import tasas as jobs_tasas

    await run_seed()
    capturado: dict[str, object] = {}

    async def _captura(**kwargs: object) -> pipeline.ReporteCorrida:
        capturado.update(kwargs)
        return pipeline.ReporteCorrida()

    monkeypatch.setattr(jobs_tasas.pipeline, "correr", _captura)

    await jobs_tasas.tasas_fetch_rapido()

    assert capturado["fetcher"] is not None
    # `cliente` no viaja: con las dos piezas puestas, `propios` sería False y
    # el pipeline dejaría Chromium vivo entre corridas.
    assert "cliente" not in capturado


async def test_the_two_passes_split_the_catalogue_without_overlapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cada fuente cae en una sola de las dos corridas.

    Si se solaparan, las dos escribirían `ultimo_hash` sobre las mismas filas y
    la barata dejaría a la del navegador viendo «sin cambios» sobre un texto
    que ella nunca renderizó.
    """
    from scheduler.jobs import tasas as jobs_tasas

    await run_seed()
    vistas: dict[str, object] = {}

    async def _captura(**kwargs: object) -> pipeline.ReporteCorrida:
        vistas[str(kwargs["solo_requieren_js"])] = kwargs["solo_requieren_js"]
        return pipeline.ReporteCorrida()

    monkeypatch.setattr(jobs_tasas.pipeline, "correr", _captura)

    await jobs_tasas.tasas_fetch_rapido()
    await jobs_tasas.tasas_fetch_navegador()

    # `None` sería «todas», y las dos pasadas pisándose.
    assert set(vistas.values()) == {False, True}

    rapidas = {f[1] for f in await pipeline._fuentes(False)}
    lentas = {f[1] for f in await pipeline._fuentes(True)}
    assert rapidas and lentas
    assert not (rapidas & lentas)
    # Y juntas siguen siendo el catálogo entero de nivel 2.
    assert rapidas | lentas == {f[1] for f in await pipeline._fuentes(None)}


async def test_the_browser_pass_stands_down_instead_of_reading_blind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El repliegue `tasas_fetch_solo_sin_js` omite la pasada, no la degrada.

    Armarle una cadena sin navegador la haría leer el envoltorio de cada SPA y
    contar sus fuentes como vacías: una corrida EXITOSA que no leyó nada.
    """
    from core.config_store import effective, set_value
    from scheduler.jobs import tasas as jobs_tasas

    await run_seed()

    async def _no_deberia(**kwargs: object) -> pipeline.ReporteCorrida:
        raise AssertionError("la pasada del navegador no debía correr")

    monkeypatch.setattr(jobs_tasas.pipeline, "correr", _no_deberia)
    await set_value("tasas_fetch_solo_sin_js", "true", actor="test")
    await effective.refresh()
    try:
        await jobs_tasas.tasas_fetch_navegador()
    finally:
        await set_value("tasas_fetch_solo_sin_js", "false", actor="test")
        await effective.refresh()

    async with session_scope() as session:
        corrida = await session.scalar(
            select(JobRun)
            .where(JobRun.job_id == jobs_tasas.JOB_ID_NAVEGADOR)
            .order_by(JobRun.id.desc())
        )
    assert corrida is not None
    assert corrida.estado is EstadoJob.OMITIDO


async def test_the_job_fails_when_every_source_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Una llave de LLM vacía producía EXITOSO con dieciocho fallos idénticos.

    El peor estado es el que parece sano: si nada funcionó, la fila de
    `job_runs` tiene que decir FALLIDO — y con las métricas dentro, que la
    bitácora persiste en su finally aunque el job lance.
    """
    from scheduler.jobs import tasas as jobs_tasas

    await run_seed()

    async def _todo_fallo(**kwargs: object) -> pipeline.ReporteCorrida:
        return pipeline.ReporteCorrida(
            fuentes=3, fallidas=3, errores=["Finsus: ErrorProveedor: no hay API key"]
        )

    monkeypatch.setattr(jobs_tasas.pipeline, "correr", _todo_fallo)

    with pytest.raises(RuntimeError, match="3 fuentes fallaron"):
        await jobs_tasas.tasas_fetch_rapido()

    async with session_scope() as session:
        corrida = await session.scalar(
            select(JobRun).where(JobRun.job_id == jobs_tasas.JOB_ID).order_by(JobRun.id.desc())
        )
    assert corrida is not None
    assert corrida.estado is EstadoJob.FALLIDO
    assert corrida.metricas is not None and corrida.metricas["fallidas"] == 3


async def test_a_total_failure_marks_the_cli_run_as_failed_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La persona delante de la terminal recibe el render, no un traceback."""
    from cli import tasas as cli_tasas

    await run_seed()

    async def _todo_fallo(**kwargs: object) -> pipeline.ReporteCorrida:
        return pipeline.ReporteCorrida(fuentes=2, fallidas=2)

    monkeypatch.setattr(cli_tasas.pipeline, "correr", _todo_fallo)

    reporte = await cli_tasas.correr_fetch(sin_navegador=True)

    assert reporte.fracaso_total
    async with session_scope() as session:
        corrida = await session.scalar(
            select(JobRun)
            .where(JobRun.job_id == cli_tasas.JOB_ID_FETCH_MANUAL)
            .order_by(JobRun.id.desc())
        )
    assert corrida is not None
    assert corrida.estado is EstadoJob.FALLIDO
    assert corrida.metricas is not None and corrida.metricas["motivo_fallo"]


async def test_the_hot_kill_switch_skips_the_job() -> None:
    from core.config_store import effective, set_value
    from scheduler.jobs.tasas import JOB_ID, tasas_fetch_rapido

    await run_seed()
    await set_value("tasas_fetch_enabled", "false", actor="test")
    await effective.refresh()
    try:
        await tasas_fetch_rapido()
    finally:
        await set_value("tasas_fetch_enabled", "true", actor="test")
        await effective.refresh()

    async with session_scope() as session:
        corrida = await session.scalar(
            select(JobRun).where(JobRun.job_id == JOB_ID).order_by(JobRun.id.desc())
        )
    assert corrida is not None
    assert corrida.estado is EstadoJob.OMITIDO
    assert "tasas_fetch_enabled" in str(corrida.metricas)


def test_the_job_is_registered_with_a_lock_long_enough_for_the_backoff() -> None:
    """Si el lock caduca a media corrida, otra instancia empieza encima."""
    from scheduler.jobs.tasas import JOB_ID
    from scheduler.registry import build_registry

    spec = next(job for job in build_registry() if job.id == JOB_ID)

    assert spec.enabled is True
    # El backoff temporal puede sumar 25 minutos por sí solo.
    assert spec.lock_ttl_seconds is not None
    assert spec.lock_ttl_seconds >= 1500


async def test_same_type_and_term_without_amounts_is_still_a_catalogue_gap() -> None:
    """Dos tasas del mismo (tipo, plazo) sin montos no son reconstruibles.

    Sin `monto_minimo` no se sabe dónde corta cada tramo, y elegir una sería
    publicar el «hasta 13%» que el extractor tiene prohibido inventar. El caso
    ambiguo sigue siendo hueco; el reconstruible es el test de abajo.
    """
    await _solo_una_fuente()
    modelo = ModeloFalso(
        tasas=[
            {"producto": "Tramo 1", "tipo": "PLAZO", "plazo_dias": 364, "tasa_nominal": "13.00"},
            {"producto": "Tramo 2", "tipo": "PLAZO", "plazo_dias": 364, "tasa_nominal": "7.00"},
        ]
    )

    reporte = await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", PAGINA)), cliente=ClienteLLM(modelo)
    )

    assert reporte.publicadas == 0
    assert reporte.en_revision == 0
    assert len(reporte.huecos_catalogo) == 2
    assert {h["tasa_nominal"] for h in reporte.huecos_catalogo} == {"13.00", "7.00"}


async def test_amount_tiers_with_distinct_floors_become_one_ladder() -> None:
    """El caso Openbank deja de ser hueco: dos entradas con montos distintos
    son UNA observación con su escalera, encolada como primera lectura."""
    await _solo_una_fuente()
    modelo = ModeloFalso(
        tasas=[
            {
                "producto": "Plazo fijo",
                "tipo": "PLAZO",
                "plazo_dias": 364,
                "tasa_nominal": "13.00",
                "monto_minimo": "0",
            },
            {
                "producto": "Plazo fijo",
                "tipo": "PLAZO",
                "plazo_dias": 364,
                "tasa_nominal": "6.30",
                "monto_minimo": "30000",
            },
        ]
    )

    reporte = await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", PAGINA)), cliente=ClienteLLM(modelo)
    )

    assert reporte.huecos_catalogo == []
    assert reporte.en_revision == 1  # una fila de reporte, no una por tramo
    assert reporte.publicadas == 0

    async with session_scope() as session:
        tasa = (await session.execute(select(Tasa))).scalars().one()
        assert tasa.tasa_nominal == Decimal("13.00")
        assert tasa.estado is EstadoTasa.PENDIENTE_REVISION
        assert [(t.desde, t.hasta, t.tasa_nominal) for t in tasa.tramos] == [
            (Decimal("0.00"), Decimal("30000.00"), Decimal("13.0000")),
            (Decimal("30000.00"), None, Decimal("6.3000")),
        ]


async def test_a_ladder_that_does_not_start_at_zero_is_a_catalogue_gap() -> None:
    """Una escalera que no cubre desde el primer peso tiene un tramo base que
    la página no declaró — y la regla 1 prohíbe inventarlo."""
    await _solo_una_fuente()
    modelo = ModeloFalso(
        tasas=[
            {
                "producto": "Plazo fijo",
                "tipo": "PLAZO",
                "plazo_dias": 364,
                "tasa_nominal": "13.00",
                "monto_minimo": "1000",
            },
            {
                "producto": "Plazo fijo",
                "tipo": "PLAZO",
                "plazo_dias": 364,
                "tasa_nominal": "6.30",
                "monto_minimo": "30000",
            },
        ]
    )

    reporte = await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", PAGINA)), cliente=ClienteLLM(modelo)
    )

    assert reporte.en_revision == 0
    assert len(reporte.huecos_catalogo) == 2


async def test_an_unexpected_failure_in_one_source_spares_the_rest() -> None:
    """Una violación de clave única con Openbank se llevó dos fuentes sanas."""
    await run_seed()
    async with session_scope() as session:
        fuentes = (await session.execute(select(FuenteTasas))).scalars().all()
        activas = {"https://www.finsus.mx/inversion", "https://www.supertasas.com/"}
        for fuente in fuentes:
            fuente.activa = fuente.url in activas

    class ModeloQueRevienta(ModeloFalso):
        async def completar(self, **kwargs: object) -> RespuestaLLM:
            self.llamadas += 1
            if self.llamadas == 1:
                raise RuntimeError("algo inesperado")
            return await super().completar(**kwargs)

    modelo = ModeloQueRevienta(tasas=[TASA_364])
    reporte = await pipeline.correr(
        fetcher=_fetcher(TransporteFalso("httpx", PAGINA, PAGINA)), cliente=ClienteLLM(modelo)
    )

    assert reporte.fallidas == 1
    assert reporte.leidas == 2  # la segunda fuente sí se procesó
    assert any("RuntimeError" in e for e in reporte.errores)
