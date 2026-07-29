"""Segundo eslabón de la cadena: un navegador de verdad.

Existe por dos motivos que se ven en el catálogo real:

- **Páginas que se pintan con JavaScript.** Ocho de las dieciocho fuentes
  devuelven un `<div id="root">` vacío a un cliente HTTP: la tabla de tasas la
  monta el navegador. `trafilatura` sobre ese HTML no encuentra nada, y con
  razón — no hay nada.
- **Páginas que rechazan al cliente plano.** Finsus devolvió 403 a httpx y su
  tabla completa a un navegador, sin cambiar nada más. No es evasión: es la
  misma petición desde un cliente que sí ejecuta lo que el sitio espera.

Se abre un navegador por corrida, no por página: arrancar Chromium cuesta más
que descargar cualquiera de estas páginas.

Playwright es un extra (`[browser]`) y sólo se instala donde hace falta. Si no
está, este transporte se declara indisponible con un mensaje que dice qué
instalar, en vez de reventar la corrida entera — el resto de la cadena sigue
funcionando y las páginas que sí rinden por httpx se leen igual.
"""

from __future__ import annotations

from typing import Any

from core.logging import get_logger
from rates_agent.fetcher import ErrorDescarga

log = get_logger(__name__)

#: Sin esto Playwright devuelve el HTML antes de que la tabla exista. No se
#: espera a `networkidle`: varias de estas páginas tienen analítica que sondea
#: sin parar y `networkidle` no llegaría nunca.
ESTADO_DE_ESPERA = "domcontentloaded"


class TransporteNavegador:
    """Carga la página en Chromium headless y devuelve el HTML ya renderizado."""

    nombre = "navegador"

    def __init__(
        self,
        *,
        user_agent: str,
        espera_red_ms: int = 2500,
    ) -> None:
        self._user_agent = user_agent
        self._espera_red_ms = espera_red_ms
        self._playwright: Any = None
        self._navegador: Any = None

    async def _arrancar(self) -> Any:
        if self._navegador is not None:
            return self._navegador
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover — depende del entorno
            raise ErrorDescarga(
                "playwright no está instalado: pip install '.[browser]' "
                "&& playwright install chromium",
                transitorio=False,
            ) from exc

        self._playwright = await async_playwright().start()
        self._navegador = await self._playwright.chromium.launch(headless=True)
        log.info("navegador_arrancado")
        return self._navegador

    async def obtener(self, url: str, *, timeout_s: float) -> str:
        navegador = await self._arrancar()
        contexto = await navegador.new_context(
            user_agent=self._user_agent,
            locale="es-MX",
            # Sin imágenes ni fuentes: sólo hace falta el DOM, y descargarlas
            # multiplica el tiempo y el ancho de banda que se le pide al sitio.
            viewport={"width": 1280, "height": 900},
        )
        try:
            await contexto.route(
                "**/*",
                lambda ruta: (
                    ruta.abort()
                    if ruta.request.resource_type in ("image", "font", "media")
                    else ruta.continue_()
                ),
            )
            pagina = await contexto.new_page()
            respuesta = await pagina.goto(
                url, wait_until=ESTADO_DE_ESPERA, timeout=timeout_s * 1000
            )
            if respuesta is not None and respuesta.status >= 400:
                raise ErrorDescarga(
                    f"HTTP {respuesta.status}",
                    transitorio=respuesta.status in (429, 500, 502, 503, 504),
                )
            # Un respiro para que lo que se pinta después del DOM aparezca. Es
            # burdo comparado con esperar un selector, pero cada institución
            # pinta la tabla de una forma distinta y no hay un selector común
            # que esperar — que es justo el motivo por el que la extracción la
            # hace un LLM y no un selector CSS.
            await pagina.wait_for_timeout(self._espera_red_ms)
            # `str()` explícito: playwright no trae tipos y `content()` llega
            # como Any, que mypy en estricto no deja pasar por un `-> str`.
            return str(await pagina.content())
        except ErrorDescarga:
            raise
        except Exception as exc:  # noqa: BLE001 — playwright tiene su jerarquía
            mensaje = str(exc)[:200]
            raise ErrorDescarga(
                f"navegador: {mensaje}",
                transitorio="timeout" in mensaje.lower(),
            ) from exc
        finally:
            await contexto.close()

    async def cerrar(self) -> None:
        if self._navegador is not None:
            await self._navegador.close()
            self._navegador = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
            log.info("navegador_cerrado")


__all__ = ["ESTADO_DE_ESPERA", "TransporteNavegador"]
