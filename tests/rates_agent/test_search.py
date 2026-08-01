"""Tests del ejecutor de búsqueda: la cadena, el circuito y las URLs vistas."""

from __future__ import annotations

from rates_agent.search import (
    ErrorBusqueda,
    ReporteBusqueda,
    Resultado,
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


def _ejecutor(*motores: MotorFalso, **extra: int) -> SearchExecutor:
    return SearchExecutor(
        list(motores),  # type: ignore[arg-type]
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
