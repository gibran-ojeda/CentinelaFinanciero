"""Tests del ejecutor de búsqueda: la cadena, el circuito y las URLs vistas."""

from __future__ import annotations

from time import monotonic

from rates_agent.search import (
    ErrorBusqueda,
    ReporteBusqueda,
    Resultado,
    SaludMotores,
    SearchExecutor,
)


class MotorFalso:
    """Devuelve, falla o se calla, según se le pida."""

    def __init__(
        self,
        nombre: str,
        *,
        urls: list[str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.nombre = nombre
        self._urls = urls or []
        self._error = error
        self.llamadas = 0

    async def buscar(self, consulta: str, *, maximo: int) -> list[Resultado]:
        self.llamadas += 1
        if self._error is not None:
            raise self._error
        return [
            Resultado(titulo=f"t{i}", url=u, resumen="r", motor=self.nombre)
            for i, u in enumerate(self._urls[:maximo])
        ]


def _salud(umbral: int | None = None) -> SaludMotores:
    # Sin pausa: el ritmo entre consultas protege a los buscadores del mundo
    # real, no a `MotorFalso`, y aquí sólo serviría para alargar la suite.
    return SaludMotores(umbral=umbral, pausa_s=0.0)


def _ejecutor(
    *motores: MotorFalso, salud: SaludMotores | None = None, **extra: int
) -> SearchExecutor:
    umbral = extra.pop("umbral_circuito", None)
    return SearchExecutor(
        list(motores),  # type: ignore[arg-type]
        salud=salud or _salud(umbral),
        espera_base_s=0.001,
        espera_tope_s=0.002,
        **extra,  # type: ignore[arg-type]
    )


# ─── La cadena ────────────────────────────────────────────────


async def test_the_first_engine_that_answers_wins() -> None:
    primero = MotorFalso("uno", urls=["https://a.test/1"])
    segundo = MotorFalso("dos", urls=["https://b.test/1"])

    resultados = await _ejecutor(primero, segundo, max_reintentos=0).buscar("tasas")

    assert [r.url for r in resultados] == ["https://a.test/1"]
    assert segundo.llamadas == 0


async def test_a_failing_engine_falls_through_to_the_next() -> None:
    caido = MotorFalso("uno", error=ErrorBusqueda("bloqueado"))
    sano = MotorFalso("dos", urls=["https://b.test/1"])

    resultados = await _ejecutor(caido, sano, max_reintentos=0).buscar("tasas")

    assert [r.url for r in resultados] == ["https://b.test/1"]


async def test_an_empty_engine_also_falls_through_without_opening_the_circuit() -> None:
    """Vacío no es error: el motor contestó bien y no había nada."""
    vacio = MotorFalso("uno", urls=[])
    sano = MotorFalso("dos", urls=["https://b.test/1"])
    ejecutor = _ejecutor(vacio, sano, max_reintentos=0)

    await ejecutor.buscar("tasas")

    assert ejecutor.motores_en_circuito == []


async def test_all_engines_down_degrades_instead_of_raising() -> None:
    """La corrida termina sin publicar, que es mejor que inventar."""
    ejecutor = _ejecutor(
        MotorFalso("uno", error=ErrorBusqueda("no")),
        MotorFalso("dos", error=ErrorBusqueda("tampoco")),
        max_reintentos=0,
    )

    assert await ejecutor.buscar("tasas") == []


# ─── Reintento y circuito ─────────────────────────────────────


async def test_a_transient_failure_is_retried() -> None:
    motor = MotorFalso("uno", error=ErrorBusqueda("timeout", transitorio=True))

    await _ejecutor(motor, max_reintentos=2).buscar("tasas")

    assert motor.llamadas == 3


async def test_a_hard_failure_is_not_retried() -> None:
    motor = MotorFalso("uno", error=ErrorBusqueda("falta la librería", transitorio=False))

    await _ejecutor(motor, max_reintentos=2).buscar("tasas")

    assert motor.llamadas == 1


async def test_two_failures_open_the_circuit_for_the_run() -> None:
    """No tiene sentido martillar a un motor que está bloqueando."""
    motor = MotorFalso("uno", error=ErrorBusqueda("429"))
    ejecutor = _ejecutor(motor, max_reintentos=0, umbral_circuito=2)

    await ejecutor.buscar("una")
    await ejecutor.buscar("otra")
    llamadas_antes = motor.llamadas
    await ejecutor.buscar("tercera")

    assert ejecutor.motores_en_circuito == ["uno"]
    assert motor.llamadas == llamadas_antes


async def test_the_circuit_survives_between_institutions() -> None:
    """El arreglo de las 116 búsquedas para un hallazgo.

    El circuito viajaba dentro del ejecutor, y hay un ejecutor por institución:
    las quince del 2026-08-02 empezaron de cero contra unos buscadores que ya
    habían devuelto 403 y 429. Ahora la salud se comparte y el segundo ejecutor
    hereda lo que aprendió el primero.
    """
    salud = _salud(umbral=2)
    primera = MotorFalso("uno", error=ErrorBusqueda("429"))
    ejecutor = _ejecutor(primera, salud=salud, max_reintentos=0)
    await ejecutor.buscar("una")
    await ejecutor.buscar("otra")
    assert ejecutor.motores_en_circuito == ["uno"]

    segunda = MotorFalso("uno", error=ErrorBusqueda("429"))
    siguiente = _ejecutor(segunda, salud=salud, max_reintentos=0)

    assert siguiente.sin_motores_sanos is True
    assert await siguiente.buscar("de otra institución") == []
    assert segunda.llamadas == 0  # ni se le pregunta


async def test_sharing_the_circuit_does_not_share_the_allowed_urls() -> None:
    """La invariante anti-alucinación no se toca.

    Es la mitad que **sí** debe reiniciarse: una URL que salió buscando Klar no
    autoriza un hallazgo de Stori. Compartir el circuito y compartir las URLs
    vistas son cosas distintas, y hasta ahora viajaban juntas por accidente.
    """
    salud = _salud()
    primera = _ejecutor(MotorFalso("uno", urls=["https://klar.test/1"]), salud=salud)
    await primera.buscar("klar")

    segunda = _ejecutor(MotorFalso("uno", urls=["https://stori.test/1"]), salud=salud)
    await segunda.buscar("stori")

    assert segunda.urls_permitidas == frozenset({"https://stori.test/1"})


async def test_a_success_forgives_the_earlier_failures() -> None:
    """Con el circuito de corrida entera, un contador que sólo sube apagaría
    un motor sano por tres tropiezos sueltos repartidos entre quince
    instituciones."""
    salud = _salud(umbral=2)
    inestable = MotorFalso("uno", error=ErrorBusqueda("hipo"))
    await _ejecutor(inestable, salud=salud, max_reintentos=0).buscar("una")

    sano = MotorFalso("uno", urls=["https://a.test/1"])
    await _ejecutor(sano, salud=salud, max_reintentos=0).buscar("otra")

    otra_vez = MotorFalso("uno", error=ErrorBusqueda("hipo"))
    ejecutor = _ejecutor(otra_vez, salud=salud, max_reintentos=0)
    await ejecutor.buscar("tercera")

    assert ejecutor.motores_en_circuito == []


async def test_consecutive_queries_to_one_engine_are_spaced_out() -> None:
    """Parte del 429 de brave lo provocaba el ritmo de las propias consultas.

    No había ninguna pausa: el tool-loop dispara las búsquedas una detrás de
    otra tan rápido como el modelo las pida.
    """
    motor = MotorFalso("uno", urls=["https://a.test/1"])
    ejecutor = _ejecutor(motor, salud=SaludMotores(pausa_s=0.05), max_reintentos=0)

    inicio = monotonic()
    await ejecutor.buscar("una")
    await ejecutor.buscar("otra")
    transcurrido = monotonic() - inicio

    assert motor.llamadas == 2
    assert transcurrido >= 0.05


# ─── Las URLs vistas ──────────────────────────────────────────


async def test_only_urls_that_really_came_back_are_allowed() -> None:
    """Es el conjunto contra el que el researcher valida sus hallazgos."""
    ejecutor = _ejecutor(MotorFalso("uno", urls=["https://a.test/1", "https://a.test/2"]))

    await ejecutor.buscar("tasas")

    assert ejecutor.urls_permitidas == frozenset({"https://a.test/1", "https://a.test/2"})


async def test_a_run_that_found_nothing_allows_nothing() -> None:
    ejecutor = _ejecutor(MotorFalso("uno", error=ErrorBusqueda("no")), max_reintentos=0)

    await ejecutor.buscar("tasas")

    assert ejecutor.urls_permitidas == frozenset()


async def test_urls_accumulate_across_searches() -> None:
    ejecutor = _ejecutor(MotorFalso("uno", urls=["https://a.test/1"]))
    await ejecutor.buscar("una")
    ejecutor._motores = [MotorFalso("dos", urls=["https://b.test/1"])]  # type: ignore[list-item]

    await ejecutor.buscar("otra")

    assert len(ejecutor.urls_permitidas) == 2


async def test_an_empty_query_is_not_a_search() -> None:
    motor = MotorFalso("uno", urls=["https://a.test/1"])
    ejecutor = _ejecutor(motor)

    assert await ejecutor.buscar("   ") == []
    assert motor.llamadas == 0
    assert ejecutor.consultas == []


def test_the_report_summarises_the_run() -> None:
    ejecutor = _ejecutor(MotorFalso("uno"))
    ejecutor.consultas.append("tasas")
    ejecutor._urls.add("https://a.test/1")

    reporte = ReporteBusqueda.de(ejecutor)

    assert (reporte.consultas, reporte.urls_vistas) == (1, 1)


def test_the_engine_chain_is_configurable() -> None:
    """Cambiar de proveedor tiene que ser una variable, no una refactorización."""
    from rates_agent.search import motores_por_defecto

    nombres = [m.nombre for m in motores_por_defecto()]

    assert nombres == ["ddgs:duckduckgo", "ddgs:google", "ddgs:brave"]


def test_the_engine_chain_is_read_hot_from_config() -> None:
    """Y además es llave caliente: la calibración la mueve sin deploy."""
    import time

    import core.config_store as cs
    from rates_agent.search import motores_por_defecto

    previo = cs._snapshot
    cs._snapshot = cs.ConfigSnapshot(
        values={"research_motores": "searxng, brave"}, loaded_at=time.monotonic()
    )
    try:
        nombres = [m.nombre for m in motores_por_defecto()]
    finally:
        cs._snapshot = previo

    assert nombres == ["ddgs:searxng", "ddgs:brave"]
