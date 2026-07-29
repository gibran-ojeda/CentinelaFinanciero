"""Tests de las cuatro capas de resiliencia del fetcher.

Cada caso de aquí corresponde a algo que pasó de verdad al intentar leer las 18
páginas del catálogo: un 403 de Finsus que el navegador sí atraviesa, un 500 de
Hey, páginas que responden 200 y no traen nada porque se pintan con JavaScript.

Lo que se verifica no es que el HTTP funcione —eso es de httpx— sino la
**política**: qué se reintenta, qué avanza la cadena, qué abre el circuito y qué
no lo abre nunca.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from rates_agent.fetcher import (
    CadenaAgotada,
    Descarga,
    ErrorDescarga,
    Fetcher,
    TransporteHttpx,
)

PAGINA = """
<html><body><article>
<h1>Inversión a plazo fijo</h1>
<p>Nuestras tasas vigentes para el mes en curso, calculadas antes de impuestos
y sujetas a cambio sin previo aviso, para montos desde cien pesos.</p>
<table><tr><td>30 días</td><td>7.19%</td></tr>
<tr><td>90 días</td><td>7.50%</td></tr>
<tr><td>180 días</td><td>7.89%</td></tr>
<tr><td>360 días</td><td>8.69%</td></tr></table>
<p>La GAT real considera la inflación estimada por el Banco de México.</p>
</article></body></html>
"""

VACIA = "<html><body><div id='root'></div><script src='app.js'></script></body></html>"


class TransporteFalso:
    """Devuelve lo que se le ponga en el guion, en orden."""

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


def _fetcher(*transportes: TransporteFalso, **kwargs: object) -> Fetcher:
    kwargs.setdefault("respetar_robots", False)
    kwargs.setdefault("esperas_backoff_s", ())
    kwargs.setdefault("espera_base_s", 0.001)
    kwargs.setdefault("espera_tope_s", 0.002)
    return Fetcher(list(transportes), **kwargs)  # type: ignore[arg-type]


URL = "https://institucion.test/inversion"


# ─── Capa 1: reintento dentro del transporte ──────────────────


async def test_a_transient_error_is_retried_and_recovers() -> None:
    t = TransporteFalso("httpx", ErrorDescarga("HTTP 503", transitorio=True), PAGINA)

    resultado = await _fetcher(t).descargar(URL)

    assert isinstance(resultado, Descarga)
    assert t.llamadas == 2
    assert resultado.intentos == 2
    assert "8.69" in resultado.texto


async def test_a_permanent_error_is_not_retried() -> None:
    """Insistirle a un 403 gasta la ventana de la corrida en algo ya decidido."""
    t = TransporteFalso("httpx", ErrorDescarga("HTTP 403", transitorio=False), PAGINA)

    with pytest.raises(CadenaAgotada):
        await _fetcher(t).descargar(URL)

    assert t.llamadas == 1


# ─── Capa 2: la cadena de transportes ─────────────────────────


async def test_a_403_advances_to_the_browser() -> None:
    """El caso Finsus: el cliente plano recibe 403 y el navegador lee la tabla."""
    plano = TransporteFalso("httpx", ErrorDescarga("HTTP 403", transitorio=False))
    navegador = TransporteFalso("navegador", PAGINA)

    resultado = await _fetcher(plano, navegador).descargar(URL)

    assert isinstance(resultado, Descarga)
    assert resultado.transporte == "navegador"


async def test_an_empty_page_advances_to_the_browser() -> None:
    """200 sin texto es una página que se pinta con JavaScript, no un fallo."""
    plano = TransporteFalso("httpx", VACIA)
    navegador = TransporteFalso("navegador", PAGINA)

    resultado = await _fetcher(plano, navegador).descargar(URL)

    assert isinstance(resultado, Descarga)
    assert resultado.transporte == "navegador"


async def test_a_chain_that_is_empty_everywhere_returns_none() -> None:
    """Sana y sin datos: se devuelve None, no se lanza."""
    f = _fetcher(TransporteFalso("httpx", VACIA), TransporteFalso("navegador", VACIA))

    assert await f.descargar(URL) is None


# ─── Capa 3: circuit breaker por host ─────────────────────────


async def test_an_empty_page_never_opens_the_circuit() -> None:
    """La distinción que sostiene todo: vacío no es error.

    Si se contara, una página sin promociones esta semana bloquearía a esa
    institución durante toda la corrida.
    """
    f = _fetcher(TransporteFalso("httpx", VACIA, VACIA, VACIA))

    for _ in range(3):
        assert await f.descargar(URL) is None

    assert f.hosts_en_circuito == []


async def test_two_hard_errors_open_the_circuit() -> None:
    duro = ErrorDescarga("HTTP 403", transitorio=False)
    f = _fetcher(TransporteFalso("httpx", duro, duro, duro), umbral_circuito=2)

    for _ in range(2):
        with pytest.raises(CadenaAgotada):
            await f.descargar(URL)

    assert f.hosts_en_circuito == ["institucion.test"]
    # Y a partir de aquí ni se intenta: no se martillea un sitio caído.
    with pytest.raises(CadenaAgotada, match="circuito abierto"):
        await f.descargar(URL)


async def test_the_circuit_is_per_host_not_global() -> None:
    """Que Finsus esté caído no puede dejar fuera a Klar."""
    # Con `max_reintentos=0`, cada descarga consume una entrada del guion: dos
    # fallos para abrir el circuito de `caida.test` y luego la página buena.
    duro = ErrorDescarga("HTTP 500", transitorio=True)
    t = TransporteFalso("httpx", duro, duro, PAGINA)
    f = _fetcher(t, umbral_circuito=2, max_reintentos=0)

    for _ in range(2):
        with pytest.raises(CadenaAgotada):
            await f.descargar("https://caida.test/tasas")

    assert f.hosts_en_circuito == ["caida.test"]
    assert isinstance(await f.descargar("https://sana.test/tasas"), Descarga)


async def test_the_half_open_reset_reopens_every_host() -> None:
    duro = ErrorDescarga("HTTP 403", transitorio=False)
    f = _fetcher(TransporteFalso("httpx", duro, duro, PAGINA), umbral_circuito=2)

    for _ in range(2):
        with pytest.raises(CadenaAgotada):
            await f.descargar(URL)
    assert f.hosts_en_circuito == ["institucion.test"]

    f.reiniciar_circuitos()

    assert f.hosts_en_circuito == []
    assert isinstance(await f.descargar(URL), Descarga)


# ─── Capa 4: backoff temporal ─────────────────────────────────


async def test_only_transient_failures_reach_the_temporal_backoff() -> None:
    """Esperar veinte minutos ante un 403 no arregla nada: es una decisión."""
    duro = ErrorDescarga("HTTP 403", transitorio=False)
    f = _fetcher(TransporteFalso("httpx", duro), esperas_backoff_s=(0.01,))

    with pytest.raises(CadenaAgotada) as exc:
        await f.descargar(URL)

    assert "backoff" not in str(exc.value)


async def test_a_transient_chain_failure_waits_and_retries() -> None:
    transitorio = ErrorDescarga("HTTP 429", transitorio=True)
    t = TransporteFalso("httpx", transitorio, transitorio, PAGINA)
    f = _fetcher(t, max_reintentos=0, esperas_backoff_s=(0.01,))

    resultado = await f.descargar(URL)

    assert isinstance(resultado, Descarga)


async def test_the_backoff_runs_out_once_and_the_run_fails_fast() -> None:
    """Agotado el backoff, el resto de la corrida no vuelve a esperar por cada URL."""
    transitorio = ErrorDescarga("HTTP 429", transitorio=True)
    t = TransporteFalso("httpx", *[transitorio] * 20)
    f = _fetcher(t, max_reintentos=0, umbral_circuito=99, esperas_backoff_s=(0.01, 0.01))

    with pytest.raises(CadenaAgotada, match="backoff agotado"):
        await f.descargar(URL)

    llamadas_tras_agotar = t.llamadas
    with pytest.raises(CadenaAgotada):
        await f.descargar("https://otra.test/tasas")
    # Un pase por la cadena y ya: ninguna espera larga más.
    assert t.llamadas == llamadas_tras_agotar + 1


# ─── Hash y robots ────────────────────────────────────────────


async def test_the_content_hash_is_stable_and_detects_change() -> None:
    """Si el hash no cambió, la corrida no gasta un token en esa página."""
    igual = await _fetcher(TransporteFalso("httpx", PAGINA)).descargar(URL)
    otra_vez = await _fetcher(TransporteFalso("httpx", PAGINA)).descargar(URL)
    distinta = await _fetcher(TransporteFalso("httpx", PAGINA.replace("8.69", "9.10"))).descargar(
        URL
    )

    assert igual is not None and otra_vez is not None and distinta is not None
    assert igual.hash_contenido == otra_vez.hash_contenido
    assert igual.hash_contenido != distinta.hash_contenido


async def test_a_disallowing_robots_is_not_an_error() -> None:
    """Es una decisión del sitio: no abre circuito y no se reintenta."""
    from urllib.robotparser import RobotFileParser

    t = TransporteFalso("httpx", PAGINA)
    f = _fetcher(t, respetar_robots=True)

    parser = RobotFileParser()
    parser.parse(["User-agent: *", "Disallow: /"])
    estado = f._estado("institucion.test")  # noqa: SLF001 — se inyecta el robots ya leído
    estado.robots = parser
    estado.robots_consultado = True

    assert await f.descargar(URL) is None
    assert t.llamadas == 0
    assert f.hosts_en_circuito == []


# ─── El transporte de navegador ───────────────────────────────


async def test_the_browser_transport_says_what_to_install_when_absent() -> None:
    """Sin playwright, ese eslabón se declara indisponible con instrucciones.

    No revienta la corrida: el resto de la cadena sigue y las páginas que sí
    rinden por httpx se leen igual.
    """
    import sys

    from rates_agent.navegador import TransporteNavegador

    if "playwright" in sys.modules:
        pytest.skip("playwright instalado: este caso cubre el entorno sin el extra")

    t = TransporteNavegador(user_agent="prueba")
    with pytest.raises(ErrorDescarga) as exc:
        await t.obtener(URL, timeout_s=1.0)

    assert "playwright install" in str(exc.value)
    assert exc.value.transitorio is False


# ─── El transporte HTTP plano ─────────────────────────────────


@respx.mock
@pytest.mark.parametrize(
    ("codigo", "transitorio"),
    [
        (429, True),  # rate limit: el tiempo lo cura
        (503, True),
        (500, True),
        (403, False),  # una decisión del servidor, no congestión
        (404, False),
        (401, False),
    ],
)
async def test_status_codes_are_classified_by_whether_time_cures_them(
    codigo: int, transitorio: bool
) -> None:
    """De esta clasificación cuelgan el reintento y el backoff temporal."""
    respx.get(URL).mock(return_value=httpx.Response(codigo))
    t = TransporteHttpx(user_agent="prueba")

    with pytest.raises(ErrorDescarga) as exc:
        await t.obtener(URL, timeout_s=1.0)

    assert exc.value.transitorio is transitorio
    await t.cerrar()


@respx.mock
async def test_a_network_timeout_is_transient() -> None:
    respx.get(URL).mock(side_effect=httpx.ConnectTimeout("agotado"))
    t = TransporteHttpx(user_agent="prueba")

    with pytest.raises(ErrorDescarga) as exc:
        await t.obtener(URL, timeout_s=1.0)

    assert exc.value.transitorio is True
    await t.cerrar()


@respx.mock
async def test_the_bot_identifies_itself_and_asks_for_spanish() -> None:
    """Se identifica con URL de contacto; no imita un navegador.

    El idioma importa: varios sitios mexicanos sirven la versión en inglés a un
    cliente sin `Accept-Language`, y ahí las tasas a veces ni aparecen.
    """
    ruta = respx.get(URL).mock(return_value=httpx.Response(200, text=PAGINA))
    agente = "Mozilla/5.0 (compatible; CentinelaFinancieroBot/1.0; +https://ejemplo/aviso)"
    t = TransporteHttpx(user_agent=agente)

    await t.obtener(URL, timeout_s=1.0)

    cabeceras = ruta.calls.last.request.headers
    assert cabeceras["user-agent"] == agente
    assert "CentinelaFinancieroBot" in cabeceras["user-agent"]
    assert cabeceras["accept-language"].startswith("es-MX")
    await t.cerrar()


@respx.mock
async def test_robots_is_read_once_per_host() -> None:
    """Una consulta por host y por corrida, no una por URL."""
    robots = respx.get("https://institucion.test/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /")
    )
    t = TransporteFalso("httpx", PAGINA, PAGINA)
    f = Fetcher([t], respetar_robots=True, esperas_backoff_s=())  # type: ignore[list-item]

    await f.descargar(URL)
    await f.descargar("https://institucion.test/otra")

    assert robots.call_count == 1
    assert t.llamadas == 2


@respx.mock
async def test_an_unreadable_robots_means_allowed() -> None:
    """Lo dice el estándar: la ausencia de reglas no es una prohibición."""
    respx.get("https://institucion.test/robots.txt").mock(return_value=httpx.Response(404))
    t = TransporteFalso("httpx", PAGINA)
    f = Fetcher([t], respetar_robots=True, esperas_backoff_s=())  # type: ignore[list-item]

    assert isinstance(await f.descargar(URL), Descarga)
