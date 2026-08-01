"""Descubrimiento y descarga de los boletines de la CNBV.

El Portafolio de Información es un SharePoint y su listado lo pinta un jqGrid
por JavaScript, así que el HTML de la página no contiene ni un enlace a
archivo. Pero el backend está abierto: la biblioteca `PortafolioInformacion`
se consulta por REST y devuelve, por elemento, su sector, tema, año, mes y el
archivo adjunto. Es de ahí de donde sale todo esto — sin navegador.

**Dos rarezas del hospedaje**, ambas descubiertas probando y ambas con su
consecuencia en el código:

1. **El Application Gateway filtra OData que parezca SQL.** `$select` y
   `$orderby` devuelven 403 —`SELECT`, `ORDER BY`— mientras que `$filter` con
   `and` y `$expand` pasan sin problema. Así que se pide el conjunto filtrado
   completo y se ordena en Python. Son unos cientos de elementos por sector,
   no un catálogo entero.

2. **El servidor manda sólo el certificado de hoja.** Falta el intermedio de
   GlobalSign, y OpenSSL —a diferencia de los navegadores y de curl en
   Windows— no lo busca por AIA. Por eso la verificación TLS falla en Linux
   con «unable to get local issuer certificate» aunque la raíz esté en el
   almacén. Se resuelve **añadiendo el intermedio** al contexto, no apagando
   la verificación: sigue exigiéndose una cadena válida hasta una raíz de
   confianza, sólo se aporta el eslabón que el servidor omite.
"""

from __future__ import annotations

import asyncio
import random
import ssl
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

import certifi
import httpx

from core.logging import get_logger
from core.settings import settings

log = get_logger(__name__)

BASE_URL = "https://portafolioinfo.cnbv.gob.mx"
BIBLIOTECA = "PortafolioInformacion"
_RUTA_LISTA = f"{BASE_URL}/_api/web/lists/getByTitle('{BIBLIOTECA}')/items"

#: El intermedio que el servidor no manda. Se versiona en el repo para que la
#: descarga no dependa de alcanzar a GlobalSign, y su huella queda auditable:
#:   sha256 B676FFA3179E8812093A1B5EAFEE876AE7A6AAF231078DAD1BFB21CD2893764A
#: Caduca en 2028; cuando la CNBV arregle su cadena, esto sobra.
CERTIFICADO_INTERMEDIO = Path(__file__).with_name("globalsign-rsa-ov-ssl-ca-2018.pem")

#: Códigos que el tiempo puede curar.
_TRANSITORIOS = frozenset({408, 425, 429, 500, 502, 503, 504})

MESES: dict[str, int] = {
    "Enero": 1,
    "Febrero": 2,
    "Marzo": 3,
    "Abril": 4,
    "Mayo": 5,
    "Junio": 6,
    "Julio": 7,
    "Agosto": 8,
    "Septiembre": 9,
    "Octubre": 10,
    "Noviembre": 11,
    "Diciembre": 12,
}


class ErrorCNBV(Exception):
    """Fallo que no mejora reintentando."""


class ErrorTransitorioCNBV(ErrorCNBV):
    """Congestión o corte pasajero."""


class BoletinNoPublicado(ErrorCNBV):
    """El periodo pedido todavía no está. No es un fallo: es el rezago."""


@dataclass(frozen=True, slots=True)
class Publicacion:
    """Un elemento de la biblioteca, ya normalizado."""

    sector: str
    tema: str
    subtema: str
    archivo: str
    ruta: str
    bytes: int
    anio: int
    mes: int

    @property
    def periodo(self) -> date:
        """Último día del mes de referencia, que es a lo que van las cifras.

        `indicadores_financieros.periodo` guarda el cierre y no el día 1: es lo
        que dice el boletín («cifras al 31 de mayo de 2026») y lo que la UI
        tiene que enseñar.
        """
        if self.mes == 12:
            return date(self.anio, 12, 31)
        return date(self.anio, self.mes + 1, 1) - timedelta(days=1)

    @property
    def url(self) -> str:
        # Los nombres de banca múltiple traen espacios («BE BM 202605.xlsx»)
        # y los de SOFIPOs guiones bajos. Se codifica siempre.
        return f"{BASE_URL}{quote(self.ruta)}"

    @property
    def extension(self) -> str:
        return self.archivo.rsplit(".", 1)[-1].lower() if "." in self.archivo else ""


def contexto_tls() -> ssl.SSLContext:
    """Verificación normal **más** el intermedio que la CNBV no manda."""
    contexto = ssl.create_default_context(cafile=certifi.where())
    contexto.load_verify_locations(str(CERTIFICADO_INTERMEDIO))
    return contexto


def _espera(intento: int, base: float, tope: float) -> float:
    return min(base * (2.0**intento), tope) * random.uniform(0.75, 1.25)  # noqa: S311


class DescargadorCNBV:
    """Consulta la biblioteca y baja archivos. Una instancia por corrida."""

    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        timeout_s: float | None = None,
        max_reintentos: int = 2,
        espera_base_s: float = 2.0,
        espera_tope_s: float = 30.0,
        cliente: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s if timeout_s is not None else settings.cnbv_timeout_seconds
        self._max_reintentos = max(0, max_reintentos)
        self._base = espera_base_s
        self._tope = espera_tope_s
        self._cliente = cliente
        self._propio = cliente is None

    def _http(self) -> httpx.AsyncClient:
        if self._cliente is None:
            self._cliente = httpx.AsyncClient(
                follow_redirects=True,
                verify=contexto_tls(),
                headers={
                    "Accept": "application/json;odata=nometadata",
                    # El mismo bot identificado del fetcher. Aquí no hay WAF
                    # que esquivar: el gateway acepta este User-Agent.
                    "User-Agent": settings.fetch_user_agent,
                },
            )
        return self._cliente

    async def publicaciones(self, *, sector: str, tema: str) -> list[Publicacion]:
        """Todo lo que la CNBV tiene de ese sector y tema, de lo nuevo a lo viejo.

        Sin `$orderby` —el WAF lo rechaza— y sin `$select` por lo mismo: se
        pide el conjunto filtrado y se ordena aquí.
        """
        url = (
            f"{self._base_url}/_api/web/lists/getByTitle('{BIBLIOTECA}')/items"
            f"?%24top=1000&%24expand=File&%24filter="
            + quote(f"Sector eq '{sector}' and Tema eq '{tema}'")
        )
        crudas: list[dict[str, object]] = []
        while url:
            cuerpo = await self._pedir_json(url)
            valores = cuerpo.get("value")
            if not isinstance(valores, list):
                raise ErrorCNBV("la respuesta del portafolio no trae 'value'")
            crudas.extend(v for v in valores if isinstance(v, dict))
            siguiente = cuerpo.get("odata.nextLink")
            url = str(siguiente) if isinstance(siguiente, str) else ""

        publicaciones = [p for p in (_convertir(c) for c in crudas) if p is not None]
        publicaciones.sort(key=lambda p: (p.anio, p.mes), reverse=True)
        log.info(
            "cnbv_publicaciones",
            sector=sector,
            tema=tema,
            total=len(publicaciones),
            ultimo=publicaciones[0].archivo if publicaciones else None,
        )
        return publicaciones

    async def ultimo(self, *, sector: str, tema: str, extension: str | None = None) -> Publicacion:
        """La publicación más reciente. `BoletinNoPublicado` si no hay ninguna.

        `extension` filtra por formato: la serie histórica mezcla `.xls`,
        `.xlsm` y `.xlsx`, y los parsers sólo leen OOXML. Pedir uno viejo en
        formato que no se sabe leer debe fallar aquí y no dentro del parser.
        """
        candidatas = await self.publicaciones(sector=sector, tema=tema)
        if extension is not None:
            candidatas = [p for p in candidatas if p.extension == extension.lower()]
        if not candidatas:
            raise BoletinNoPublicado(
                f"no hay publicaciones de {sector} / {tema}"
                + (f" en formato {extension}" if extension else "")
            )
        return candidatas[0]

    async def descargar(self, publicacion: Publicacion, destino: Path) -> Path:
        """Baja el archivo y lo deja en disco. Devuelve la ruta.

        El archivo crudo se conserva: sin el original, un indicador cargado de
        la CNBV no es auditable — sólo es un número que alguien dice que leyó.
        """
        destino.parent.mkdir(parents=True, exist_ok=True)
        contenido = await self._pedir_bytes(publicacion.url)
        destino.write_bytes(contenido)
        log.info(
            "cnbv_descargado",
            archivo=publicacion.archivo,
            kb=len(contenido) // 1024,
            destino=str(destino),
        )
        return destino

    async def _pedir_json(self, url: str) -> dict[str, object]:
        respuesta = await self._con_reintentos(url)
        try:
            cuerpo = respuesta.json()
        except ValueError as exc:
            raise ErrorCNBV("el portafolio contestó algo que no es JSON") from exc
        if not isinstance(cuerpo, dict):
            raise ErrorCNBV("el portafolio contestó un JSON que no es un objeto")
        return cuerpo

    async def _pedir_bytes(self, url: str) -> bytes:
        return (await self._con_reintentos(url)).content

    async def _con_reintentos(self, url: str) -> httpx.Response:
        ultimo: Exception | None = None
        for intento in range(1 + self._max_reintentos):
            try:
                return await self._una_vez(url)
            except ErrorTransitorioCNBV as exc:
                ultimo = exc
                if intento == self._max_reintentos:
                    break
                espera = _espera(intento, self._base, self._tope)
                log.warning(
                    "cnbv_reintento",
                    intento=intento + 1,
                    espera_s=round(espera, 1),
                    error=str(exc)[:160],
                )
                await asyncio.sleep(espera)
        assert ultimo is not None
        raise ultimo

    async def _una_vez(self, url: str) -> httpx.Response:
        try:
            respuesta = await self._http().get(url, timeout=self._timeout_s)
        except httpx.TimeoutException as exc:
            raise ErrorTransitorioCNBV(f"timeout tras {self._timeout_s}s") from exc
        except httpx.HTTPError as exc:
            detalle = str(exc).strip() or type(exc).__name__
            raise ErrorTransitorioCNBV(f"error de red: {detalle}") from exc

        if respuesta.status_code in _TRANSITORIOS:
            raise ErrorTransitorioCNBV(f"HTTP {respuesta.status_code}")
        if respuesta.status_code == 403:
            # Casi siempre es el WAF rechazando un parámetro de OData. Se dice
            # explícitamente porque el 403 pelado manda a buscar credenciales
            # que este portal no pide.
            raise ErrorCNBV(
                "HTTP 403: el gateway rechazó la consulta. El WAF filtra "
                "$select y $orderby por parecerse a SQL — se ordena en Python."
            )
        if respuesta.status_code >= 400:
            raise ErrorCNBV(f"HTTP {respuesta.status_code} en {url[:120]}")
        return respuesta

    async def cerrar(self) -> None:
        if self._cliente is not None and self._propio:
            await self._cliente.aclose()
            self._cliente = None

    async def __aenter__(self) -> DescargadorCNBV:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.cerrar()


def _convertir(cruda: dict[str, object]) -> Publicacion | None:
    """Un elemento de SharePoint a `Publicacion`, o `None` si no es usable."""
    archivo = cruda.get("File")
    if not isinstance(archivo, dict):
        return None
    nombre = str(archivo.get("Name") or "").strip()
    ruta = str(archivo.get("ServerRelativeUrl") or "").strip()
    if not nombre or not ruta:
        return None

    try:
        # SharePoint devuelve el año como texto y lo llama `A_x00f1_o`, que es
        # «Año» con la eñe escapada al estilo de sus nombres internos.
        anio = int(str(cruda.get("A_x00f1_o") or "").strip())
    except ValueError:
        return None
    mes = MESES.get(str(cruda.get("Mes") or "").strip())
    if mes is None:
        # Hay elementos con el mes vacío o con texto libre (manuales,
        # calendarios). No son boletines de un periodo y se descartan.
        return None

    try:
        tamano = int(str(archivo.get("Length") or 0))
    except ValueError:
        tamano = 0

    return Publicacion(
        sector=str(cruda.get("Sector") or "").strip(),
        tema=str(cruda.get("Tema") or "").strip(),
        subtema=str(cruda.get("SubTema") or "").strip(),
        archivo=nombre,
        ruta=ruta,
        bytes=tamano,
        anio=anio,
        mes=mes,
    )


__all__ = [
    "BASE_URL",
    "BIBLIOTECA",
    "CERTIFICADO_INTERMEDIO",
    "MESES",
    "BoletinNoPublicado",
    "DescargadorCNBV",
    "ErrorCNBV",
    "ErrorTransitorioCNBV",
    "Publicacion",
    "contexto_tls",
]
