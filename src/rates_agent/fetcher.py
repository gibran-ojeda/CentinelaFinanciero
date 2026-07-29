"""Descarga de las páginas de tasas, con las cuatro capas de resiliencia.

Portado de `backtesting/search_executor.py` de NarrativeAlpha, que lleva meses
resolviendo esta misma clase de problema en producción. Allí la cadena son
buscadores; aquí son **transportes** para una URL que no se puede cambiar:

1. **Reintento dentro del transporte** — `1 + max_reintentos` intentos, con
   backoff exponencial y jitter.
2. **Cadena de transportes** — `httpx → navegador`. Se avanza tanto si el
   eslabón falla como si trae la página vacía. Una página que rechaza a un
   cliente HTTP plano suele rendir a un navegador, que es exactamente lo que
   pasa con varias del catálogo.
3. **Circuit breaker por host y por corrida** — dos errores duros y ese host se
   deja para la semana siguiente. No tiene sentido martillar dieciocho veces un
   sitio que está caído.
4. **Backoff temporal con reset half-open** — cuando la cadena entera cae por
   algo que el tiempo arregla, se espera y se vuelve a probar todo.

**La distinción que sostiene todo esto: VACÍO no es ERROR.** Una página que
responde 200 y no trae texto útil está sana: la cadena avanza al navegador y el
circuito **no** se abre. Confundir ambas cosas convierte una página que hoy no
tiene promociones en un host bloqueado toda la corrida.

Y una adaptación deliberada respecto del original: **sólo lo transitorio entra
al backoff temporal**. Esperar veinte minutos ante un 429 tiene sentido; ante un
403 no, porque un 403 es una decisión y no una congestión. Un 403 sí avanza la
cadena — el navegador puede pasar donde el cliente plano no.

**El bot se identifica y no evade.** Se manda un User-Agent propio con URL de
contacto y se respeta `robots.txt`. Si una institución decide bloquear a un bot
identificado, esa fuente pasa a lectura manual y se registra por qué; disfrazar
el cliente para saltarse un WAF no está sobre la mesa.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from core.logging import get_logger
from core.settings import settings

log = get_logger(__name__)


class ErrorDescarga(Exception):
    """Error duro de un transporte. `transitorio` decide si el tiempo lo cura."""

    def __init__(self, mensaje: str, *, transitorio: bool = False) -> None:
        super().__init__(mensaje)
        self.transitorio = transitorio


class CadenaAgotada(ErrorDescarga):
    """Ningún transporte pudo con la URL. El mensaje enumera qué intentó cada uno."""


@dataclass(frozen=True, slots=True)
class Descarga:
    """Una página leída con éxito."""

    url: str
    texto: str
    #: sha256 del HTML crudo. Si no cambió desde la corrida anterior, no hay
    #: nada que extraer y no se gasta ni un token.
    hash_contenido: str
    transporte: str
    intentos: int


class Transporte(Protocol):
    """Una forma de traerse el HTML de una URL."""

    nombre: str

    async def obtener(self, url: str, *, timeout_s: float) -> str:
        """HTML crudo. Lanza `ErrorDescarga` si no puede."""
        ...

    async def cerrar(self) -> None: ...


# ─── Transporte HTTP plano ────────────────────────────────────

#: Códigos que el tiempo puede curar. Un 403 o un 404 no están aquí: son
#: decisiones del servidor, no congestión.
_TRANSITORIOS = frozenset({408, 425, 429, 500, 502, 503, 504})


class TransporteHttpx:
    """Cliente HTTP plano. Rápido, barato, y suficiente para la mayoría."""

    nombre = "httpx"

    def __init__(self, *, user_agent: str) -> None:
        self._user_agent = user_agent
        self._cliente: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._cliente is None:
            self._cliente = httpx.AsyncClient(
                follow_redirects=True,
                headers={
                    "User-Agent": self._user_agent,
                    # Sin esto varios sitios mexicanos sirven la versión en
                    # inglés, donde las tasas a veces ni aparecen.
                    "Accept-Language": "es-MX,es;q=0.9",
                    "Accept": "text/html,application/xhtml+xml",
                },
            )
        return self._cliente

    async def obtener(self, url: str, *, timeout_s: float) -> str:
        try:
            resp = await self._http().get(url, timeout=timeout_s)
        except httpx.TimeoutException as exc:
            raise ErrorDescarga(f"timeout tras {timeout_s}s", transitorio=True) from exc
        except httpx.HTTPError as exc:
            raise ErrorDescarga(f"error de red: {exc}", transitorio=True) from exc

        if resp.status_code >= 400:
            raise ErrorDescarga(
                f"HTTP {resp.status_code}",
                transitorio=resp.status_code in _TRANSITORIOS,
            )
        return resp.text

    async def cerrar(self) -> None:
        if self._cliente is not None:
            await self._cliente.aclose()
            self._cliente = None


# ─── El fetcher ───────────────────────────────────────────────


def _host(url: str) -> str:
    return (urlsplit(url).netloc or url).lower()


def _espera(intento: int, base: float, tope: float) -> float:
    return min(base * (2.0**intento), tope) * random.uniform(0.75, 1.25)  # noqa: S311


def _texto_legible(html: str) -> str:
    """Texto principal de la página. Cadena vacía si no hay nada que leer."""
    import trafilatura

    extraido = trafilatura.extract(
        html, include_comments=False, include_tables=True, favor_recall=True
    )
    return (extraido or "").strip()


@dataclass(slots=True)
class _EstadoHost:
    fallos: int = 0
    abierto: bool = False
    robots: RobotFileParser | None = None
    robots_consultado: bool = False


class Fetcher:
    """Descarga páginas con las cuatro capas. Una instancia por corrida.

    El estado del circuito vive en la instancia, así que se reinicia solo en
    cada corrida nueva — lo mismo que hace NarrativeAlpha con el suyo.
    """

    def __init__(
        self,
        transportes: list[Transporte] | None = None,
        *,
        user_agent: str | None = None,
        timeout_s: float | None = None,
        max_reintentos: int | None = None,
        umbral_circuito: int | None = None,
        esperas_backoff_s: tuple[float, ...] | None = None,
        min_caracteres: int | None = None,
        respetar_robots: bool | None = None,
        espera_base_s: float = 2.0,
        espera_tope_s: float = 30.0,
    ) -> None:
        self._user_agent = user_agent or settings.fetch_user_agent
        self._transportes = transportes or [TransporteHttpx(user_agent=self._user_agent)]
        self._timeout_s = timeout_s if timeout_s is not None else settings.fetch_timeout_seconds
        self._max_reintentos = (
            max_reintentos if max_reintentos is not None else settings.fetch_max_reintentos
        )
        self._umbral = (
            umbral_circuito if umbral_circuito is not None else settings.fetch_umbral_circuito
        )
        self._esperas = tuple(
            esperas_backoff_s
            if esperas_backoff_s is not None
            else settings.fetch_esperas_backoff_s
        )
        self._min_caracteres = (
            min_caracteres if min_caracteres is not None else settings.fetch_min_caracteres
        )
        self._respetar_robots = (
            respetar_robots if respetar_robots is not None else settings.fetch_respetar_robots
        )
        self._base = espera_base_s
        self._tope = espera_tope_s
        self._hosts: dict[str, _EstadoHost] = {}
        #: El backoff temporal ya se agotó en esta corrida: no se vuelve a
        #: esperar por cada URL restante.
        self._backoff_agotado = False
        self._lock_backoff = asyncio.Lock()
        log.info(
            "fetcher_init",
            transportes=[t.nombre for t in self._transportes],
            umbral_circuito=self._umbral,
            esperas_backoff_s=list(self._esperas),
        )

    @property
    def hosts_en_circuito(self) -> list[str]:
        return sorted(h for h, e in self._hosts.items() if e.abierto)

    def _estado(self, host: str) -> _EstadoHost:
        return self._hosts.setdefault(host, _EstadoHost())

    def reiniciar_circuitos(self) -> None:
        """Half-open: vuelve a permitir todos los hosts para reprobar la cadena."""
        abiertos = self.hosts_en_circuito
        if abiertos:
            log.info("fetcher_circuitos_reiniciados", hosts=abiertos)
        for estado in self._hosts.values():
            estado.fallos = 0
            estado.abierto = False

    # ── robots.txt ──

    async def _permitido(self, url: str) -> bool:
        if not self._respetar_robots:
            return True
        host = _host(url)
        estado = self._estado(host)
        if not estado.robots_consultado:
            estado.robots = await self._leer_robots(url)
            estado.robots_consultado = True
        if estado.robots is None:
            # Sin robots.txt legible se asume permitido, que es lo que dice el
            # estándar: la ausencia de reglas no es una prohibición.
            return True
        return estado.robots.can_fetch(self._user_agent, url)

    async def _leer_robots(self, url: str) -> RobotFileParser | None:
        partes = urlsplit(url)
        destino = f"{partes.scheme}://{partes.netloc}/robots.txt"
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, headers={"User-Agent": self._user_agent}
            ) as cliente:
                resp = await cliente.get(destino, timeout=10.0)
        except httpx.HTTPError as exc:
            log.info("robots_ilegible", url=destino, error=str(exc)[:120])
            return None
        if resp.status_code >= 400:
            return None
        parser = RobotFileParser()
        parser.parse(resp.text.splitlines())
        return parser

    # ── La cadena ──

    async def descargar(self, url: str) -> Descarga | None:
        """Página leída, o `None` si toda la cadena respondió sana y vacía.

        Raises:
            CadenaAgotada: nadie trajo datos y la cadena quedó degradada.
        """
        try:
            return await self._recorrer_cadena(url)
        except CadenaAgotada as exc:
            # El backoff temporal se aplica **desde fuera** de la cadena y no
            # dentro: si `_recorrer_cadena` se llamara a sí misma a través de
            # `descargar`, la segunda vuelta intentaría tomar un lock que la
            # primera ya tiene y la corrida se quedaría colgada ahí.
            if not (exc.transitorio and self._esperas) or self._backoff_agotado:
                raise
            return await self._con_backoff_temporal(url, str(exc))

    async def _recorrer_cadena(self, url: str) -> Descarga | None:
        """Un pase por la cadena de transportes. Sin esperas largas."""
        host = _host(url)
        estado = self._estado(host)

        if estado.abierto:
            raise CadenaAgotada(f"{host}: circuito abierto en esta corrida")

        if not await self._permitido(url):
            # No es un error: es una decisión del sitio. No abre circuito y no
            # se reintenta — se registra y esa fuente pasa a lectura manual.
            log.warning("fetch_robots_prohibe", url=url, user_agent=self._user_agent)
            return None

        intentos: list[tuple[str, str]] = []
        degradada = False
        transitoria = False

        for transporte in self._transportes:
            try:
                html, n = await self._con_reintentos(transporte, url)
            except ErrorDescarga as exc:
                self._contar_fallo(host)
                intentos.append((transporte.nombre, str(exc)[:160]))
                degradada = True
                transitoria = transitoria or exc.transitorio
                continue

            texto = _texto_legible(html)
            if len(texto) >= self._min_caracteres:
                log.info(
                    "fetch_ok",
                    url=url,
                    transporte=transporte.nombre,
                    caracteres=len(texto),
                    intentos=n,
                )
                return Descarga(
                    url=url,
                    texto=texto,
                    hash_contenido=hashlib.sha256(html.encode("utf-8")).hexdigest(),
                    transporte=transporte.nombre,
                    intentos=n,
                )
            # VACÍO: la página contestó bien pero no hay texto que leer. Suele
            # ser una que se pinta con JavaScript, así que la cadena avanza —
            # sin tocar el circuito, porque el servidor no hizo nada mal.
            intentos.append((transporte.nombre, f"vacío ({len(texto)} caracteres)"))

        if not degradada:
            log.info("fetch_vacio", url=url, intentos=[t for t, _ in intentos])
            return None

        resumen = "; ".join(f"{t} → {d}" for t, d in intentos)
        raise CadenaAgotada(f"{url}: {resumen}", transitorio=transitoria)

    async def _con_reintentos(self, transporte: Transporte, url: str) -> tuple[str, int]:
        """`1 + max_reintentos` intentos contra un transporte."""
        ultimo: ErrorDescarga | None = None
        for intento in range(1 + self._max_reintentos):
            try:
                return await transporte.obtener(url, timeout_s=self._timeout_s), intento + 1
            except ErrorDescarga as exc:
                ultimo = exc
                # Sólo se reintenta lo que el tiempo puede curar. Insistirle a
                # un 403 es gastar la ventana de la corrida en algo decidido.
                if not exc.transitorio or intento == self._max_reintentos:
                    break
                espera = _espera(intento, self._base, self._tope)
                log.warning(
                    "fetch_reintento",
                    url=url,
                    transporte=transporte.nombre,
                    intento=intento + 1,
                    espera_s=round(espera, 1),
                    error=str(exc)[:120],
                )
                await asyncio.sleep(espera)
        assert ultimo is not None
        raise ultimo

    def _contar_fallo(self, host: str) -> None:
        estado = self._estado(host)
        estado.fallos += 1
        if estado.fallos >= self._umbral and not estado.abierto:
            estado.abierto = True
            log.warning("fetch_circuito_abierto", host=host, fallos=estado.fallos)

    async def _con_backoff_temporal(self, url: str, resumen: str) -> Descarga | None:
        """Espera y reprueba la cadena entera, con reset half-open.

        Serializado por un lock: si diez URLs caen a la vez, se espera una vez y
        no diez. Al agotarse, el resto de la corrida falla rápido en vez de
        volver a esperar veinte minutos por cada una.
        """
        async with self._lock_backoff:
            if self._backoff_agotado:
                raise CadenaAgotada(f"{url}: {resumen} (backoff ya agotado en esta corrida)")

            # Otra URL pudo haber esperado y recuperado el host mientras ésta
            # aguardaba el lock: se reprueba antes de dormir de nuevo.
            self.reiniciar_circuitos()
            try:
                return await self._recorrer_cadena(url)
            except CadenaAgotada:
                pass

            for i, espera in enumerate(self._esperas):
                log.warning(
                    "fetch_backoff",
                    url=url,
                    intento=i + 1,
                    de=len(self._esperas),
                    espera_s=espera,
                )
                await asyncio.sleep(espera)
                self.reiniciar_circuitos()
                try:
                    return await self._recorrer_cadena(url)
                except CadenaAgotada:
                    continue

            self._backoff_agotado = True
            raise CadenaAgotada(f"{url}: {resumen} (backoff agotado)")

    async def cerrar(self) -> None:
        for transporte in self._transportes:
            await transporte.cerrar()

    async def __aenter__(self) -> Fetcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.cerrar()


__all__ = [
    "CadenaAgotada",
    "Descarga",
    "ErrorDescarga",
    "Fetcher",
    "Transporte",
    "TransporteHttpx",
]
