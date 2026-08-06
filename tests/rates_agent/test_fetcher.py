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
    SinTransporteCapaz,
    TransporteHttpx,
    _texto_legible,
    una_linea,
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

    def __init__(
        self, nombre: str, *guion: str | ErrorDescarga, renderiza_js: bool | None = None
    ) -> None:
        self.nombre = nombre
        # Por defecto renderiza el que se llama «navegador», que es como se
        # nombra en el resto de los tests.
        self.renderiza_js = nombre == "navegador" if renderiza_js is None else renderiza_js
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


# ─── La segunda pasada de extracción ──────────────────────────

#: Una tabla montada con divs de maquetación, que es como la publican
#: Crediclub, kubo y Finsus. `trafilatura.extract` la descarta entera junto con
#: el menú: la cifra está en el HTML y no llega al texto.
TABLA_EN_DIVS = """
<html><body>
<nav><a href="/">Inicio</a><a href="/creditos">Créditos</a></nav>
<div class="hero"><h1>Invierte con nosotros</h1>
<p>Somos una institución regulada por la CNBV y tu dinero está protegido por el
fondo de protección al ahorro. Abre tu cuenta desde la app en minutos, sin
comisiones por manejo de cuenta ni saldo mínimo, y empieza a ver crecer tus
ahorros desde el primer peso que deposites con nosotros hoy mismo.</p></div>
<div class="tasas"><div class="fila"><span>364 días</span><span>8.80%</span></div>
<div class="fila"><span>A la vista</span><span>6.30%</span></div></div>
<footer>Aviso de privacidad</footer>
</body></html>
"""


def test_a_rate_table_the_filter_discards_is_rescued() -> None:
    """Medido el 2026-08-06 en Crediclub, kubo y Finsus.

    Las cifras estaban en el HTML —y también en el DOM renderizado, así que no
    era cosa del navegador— y ninguna llegaba al texto: `extract` se lleva por
    delante lo que no reconoce como cuerpo del artículo.
    """
    texto = _texto_legible(TABLA_EN_DIVS)

    assert "8.80%" in texto
    assert "6.30%" in texto


def test_the_clean_reading_wins_when_it_already_has_the_rates() -> None:
    """La segunda pasada arrastra menú y pie: sólo entra si hace falta."""
    texto = _texto_legible(PAGINA)

    assert "8.69" in texto
    # `PAGINA` es un artículo con su tabla dentro, que `extract` sí conserva:
    # el rescate no debería haberse disparado y con él no vendría el `<nav>`.
    assert "Inicio" not in texto


def test_a_page_with_no_rates_anywhere_stays_empty() -> None:
    """Sin porcentajes en ninguna de las dos pasadas, no hay nada que rescatar."""
    assert _texto_legible(VACIA).strip() == ""


# ─── La marca `requiere_js` manda sobre el transporte ─────────


async def test_a_js_source_is_not_resolved_by_the_plain_client() -> None:
    """El caso Crediclub, y la razón de que el comparador siguiera vacío.

    httpx devuelve el shell de la SPA —nav, pie, aviso legal, copy de
    marketing— que pasa de sobra el umbral de caracteres. La cadena daba la
    descarga por buena, Chromium no llegaba a abrirse, y el extractor leía una
    página perfectamente legible que no contenía ninguna tabla de tasas.
    """
    plano = TransporteFalso("httpx", PAGINA)  # texto de sobra, y aun así no gana
    navegador = TransporteFalso("navegador", PAGINA.replace("8.69", "9.99"))

    resultado = await _fetcher(plano, navegador).descargar(URL, requiere_js=True)

    assert isinstance(resultado, Descarga)
    assert resultado.transporte == "navegador"
    assert plano.llamadas == 0
    assert "9.99" in resultado.texto


async def test_a_source_without_the_mark_still_prefers_the_cheap_client() -> None:
    """Lo barato primero sigue siendo la regla para todo lo demás."""
    plano = TransporteFalso("httpx", PAGINA)
    navegador = TransporteFalso("navegador", PAGINA)

    resultado = await _fetcher(plano, navegador).descargar(URL)

    assert isinstance(resultado, Descarga)
    assert resultado.transporte == "httpx"
    assert navegador.llamadas == 0


async def test_a_js_source_in_a_chain_without_a_browser_says_so() -> None:
    """Una fuente JS en la corrida barata es un error de reparto, no suyo.

    Tiene que distinguirse de una fuente caída: si saliera como fallo normal,
    el contador de salud la acercaría a la autopausa por algo que decidió el
    scheduler, y acabaríamos apagando páginas que funcionan.
    """
    plano = TransporteFalso("httpx", PAGINA)
    f = _fetcher(plano)

    with pytest.raises(SinTransporteCapaz, match="necesita renderizado"):
        await f.descargar(URL, requiere_js=True)

    assert plano.llamadas == 0
    assert f.hosts_en_circuito == []


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


@respx.mock
async def test_an_html_robots_is_not_parsed_as_rules() -> None:
    """El caso DiDi: su robots.txt redirige a una página de 404 que da 200.

    `RobotFileParser` sobre HTML produce un parser vacío, que permite todo — la
    respuesta correcta, pero por accidente. Se descarta explícitamente para que
    «no hay reglas» y «no llegué a leer reglas» dejen de ser lo mismo.
    """
    respx.get("https://institucion.test/robots.txt").mock(
        return_value=httpx.Response(200, html="<html><body>Página no encontrada</body></html>")
    )
    f = Fetcher([TransporteFalso("httpx", PAGINA)], respetar_robots=True)  # type: ignore[list-item]

    parser = await f._leer_robots(URL)  # noqa: SLF001 — es lo que se prueba

    assert parser is None
    # Y el efecto es el del estándar: sin reglas legibles, se permite.
    assert await f._permitido(URL) is True  # noqa: SLF001


@respx.mock
async def test_a_plain_text_robots_is_still_honoured() -> None:
    """Lo que no puede pasar es dejar de respetar un robots.txt de verdad."""
    respx.get("https://institucion.test/robots.txt").mock(
        return_value=httpx.Response(
            200,
            text="User-agent: *\nDisallow: /",
            headers={"content-type": "text/plain; charset=utf-8"},
        )
    )
    f = Fetcher([TransporteFalso("httpx", PAGINA)], respetar_robots=True)  # type: ignore[list-item]

    assert await f._permitido(URL) is False  # noqa: SLF001


# ─── El transporte de navegador ───────────────────────────────


async def test_the_browser_transport_says_what_to_install_when_absent() -> None:
    """Sin playwright, ese eslabón se declara indisponible con instrucciones.

    No revienta la corrida: el resto de la cadena sigue y las páginas que sí
    rinden por httpx se leen igual.
    """
    import importlib.util

    from rates_agent.navegador import TransporteNavegador

    # `find_spec` y no `sys.modules`: playwright no se importa hasta que el
    # transporte lo intenta, así que mirar los módulos cargados diría siempre
    # que falta — y el test pasaría sin probar nada donde sí está instalado.
    if importlib.util.find_spec("playwright") is not None:
        pytest.skip("playwright instalado: este caso cubre el entorno sin el extra")

    t = TransporteNavegador(user_agent="prueba")
    with pytest.raises(ErrorDescarga) as exc:
        await t.obtener(URL, timeout_s=1.0)

    assert "playwright install" in str(exc.value)
    assert exc.value.transitorio is False


async def test_a_browser_launch_failure_degrades_as_a_download_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un Chromium que no arranca no puede escapar como excepción cruda.

    Sin el envoltorio, una librería de sistema ausente en la imagen contaba
    cada fuente JS como fallo suelto —invisible para la cadena— y dejaba
    playwright arrancado, que es una fuga en un proceso que vive días.
    """
    import sys
    import types

    from rates_agent.navegador import TransporteNavegador

    class _ArranqueRoto:
        async def start(self) -> None:
            raise RuntimeError("error while loading shared libraries: libnss3.so")

    stub = types.ModuleType("playwright.async_api")
    stub.async_playwright = lambda: _ArranqueRoto()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", stub)

    t = TransporteNavegador(user_agent="prueba")
    with pytest.raises(ErrorDescarga) as exc:
        await t.obtener(URL, timeout_s=1.0)

    assert "no arrancó" in str(exc.value)
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
async def test_a_slow_server_is_transient() -> None:
    """La conexión se estableció y el servidor tardó: eso sí es congestión."""
    respx.get(URL).mock(side_effect=httpx.ReadTimeout("agotado"))
    t = TransporteHttpx(user_agent="prueba")

    with pytest.raises(ErrorDescarga) as exc:
        await t.obtener(URL, timeout_s=1.0)

    assert exc.value.transitorio is True
    await t.cerrar()


@respx.mock
@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("[Errno -2] Name or service not known"),
        httpx.ConnectTimeout("agotado"),
    ],
)
async def test_a_host_that_never_answers_is_not_transient(error: Exception) -> None:
    """El caso Supertasas del 2026-08-02.

    Un DNS que no resuelve o un puerto que rechaza venía marcado transitorio,
    así que entraba al backoff temporal: la corrida durmió veinticinco minutos
    con trece fuentes detrás y el host seguía caído al despertar. Esperar no
    arregla un host que no está.

    `ConnectTimeout` hereda de `TimeoutException` **y** de `TransportError`, así
    que este caso depende del orden de los `except`: si alguien reordena, este
    test es el que lo dice.
    """
    respx.get(URL).mock(side_effect=error)
    t = TransporteHttpx(user_agent="prueba")

    with pytest.raises(ErrorDescarga) as exc:
        await t.obtener(URL, timeout_s=1.0)

    assert exc.value.transitorio is False
    await t.cerrar()


# ─── El mensaje de error ──────────────────────────────────────


def test_a_message_is_flattened_and_cut_at_a_word() -> None:
    """El «Call log» de Playwright viene multilínea y acaba en una tabla."""
    crudo = 'Page.goto: net::ERR_CONNECTION_CLOSED\nCall log:\n  - navigating to "x"'

    assert "\n" not in una_linea(crudo, 500)
    assert una_linea(crudo, 500) == (
        'Page.goto: net::ERR_CONNECTION_CLOSED Call log: - navigating to "x"'
    )
    # Y el corte respeta la palabra: antes salía `waiting until "domcon`.
    assert una_linea("waiting until domcontentloaded", 20) == "waiting until…"


def test_a_message_without_spaces_is_not_cut_to_nothing() -> None:
    """Una URL larga no tiene dónde cortar: vale más truncada que vacía."""
    largo = "https://institucion.test/" + "a" * 200

    recortado = una_linea(largo, 40)

    assert len(recortado) == 41  # 40 + la elipsis
    assert recortado.startswith("https://institucion.test/")


async def test_the_error_names_the_url_once() -> None:
    """El log del 2026-08-02 la traía tres veces en la misma línea.

    `descargar` pasa `str(exc)` —que ya empieza por la URL— como resumen al
    backoff, y el backoff la anteponía otra vez. Con el nombre del transporte
    duplicado encima, el mensaje era ilegible justo donde más se lee: en
    `cli fuentes list`.
    """
    transitorio = ErrorDescarga("HTTP 503", transitorio=True)
    f = _fetcher(
        TransporteFalso("httpx", transitorio, transitorio, transitorio),
        max_reintentos=0,
        esperas_backoff_s=(0.01,),
    )

    with pytest.raises(CadenaAgotada) as exc:
        await f.descargar(URL)

    assert str(exc.value).count(URL) == 1
    assert "backoff agotado" in str(exc.value)


async def test_the_transport_name_is_not_repeated() -> None:
    """Salía `navegador → navegador: Page.goto: …`."""
    t = TransporteFalso("navegador", ErrorDescarga("navegador: Page.goto: falló"))
    f = _fetcher(t, max_reintentos=0)

    with pytest.raises(CadenaAgotada) as exc:
        await f.descargar(URL)

    assert "navegador → Page.goto: falló" in str(exc.value)


async def test_a_host_that_never_answers_still_opens_its_circuit() -> None:
    """Lo que se le niega es la espera larga, no el circuito.

    Si dejara de contar para el circuito, cada URL del mismo host muerto
    volvería a recorrer la cadena entera de transportes —incluido arrancar
    Chromium— en vez de fallar de inmediato.
    """
    muerto = ErrorDescarga("no conecta: ConnectError", transitorio=False)
    f = _fetcher(TransporteFalso("httpx", muerto, muerto), esperas_backoff_s=(60.0,))

    for _ in range(2):
        with pytest.raises(CadenaAgotada) as exc:
            await f.descargar(URL)

    assert f.hosts_en_circuito == ["institucion.test"]
    assert "backoff" not in str(exc.value)


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
