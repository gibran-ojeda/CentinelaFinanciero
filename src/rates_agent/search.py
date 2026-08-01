"""Ejecutor de búsqueda determinista para el nivel 3.

El researcher no busca: **pide** que se busque. Esa separación es lo que hace
posible la invariante anti-alucinación de §15 — si el modelo pudiera producir
URLs por su cuenta no habría forma de distinguir una fuente real de una
plausible. Aquí se ejecuta la búsqueda, se guardan las URLs que volvieron, y
después se descarta todo hallazgo cuya URL no esté en ese conjunto.

Las cuatro capas son las mismas del fetcher, y por la misma razón: llevan meses
funcionando en NarrativeAlpha contra esta misma clase de problema.

1. **Reintento dentro del motor**, con backoff exponencial y jitter.
2. **Cadena de motores** — `duckduckgo → google → brave` a través de `ddgs`.
   Se avanza tanto si el motor falla como si devuelve vacío.
3. **Circuit breaker por motor y por corrida**: dos fallos duros y ese motor se
   deja para la próxima. No tiene sentido martillar a uno que está bloqueando.
4. **La corrida degrada, no revienta.** Si todos los motores caen, `buscar`
   devuelve lista vacía y el researcher termina sin publicar nada.

**El backend es intercambiable por configuración a propósito.** `ddgs` cuesta
cero y no pide llaves, que es lo que lo hace el punto de partida correcto para
un camino ocasional de descubrimiento. Si la calibración muestra que no basta,
apuntar al SearXNG que ya corre en el VPS tiene que ser una variable de entorno
y no una refactorización — ver la nota de la fase 09.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from core.logging import get_logger
from core.settings import settings

log = get_logger(__name__)


class ErrorBusqueda(Exception):
    """El motor no pudo. `transitorio` decide si el tiempo lo cura."""

    def __init__(self, mensaje: str, *, transitorio: bool = True) -> None:
        super().__init__(mensaje)
        self.transitorio = transitorio


@dataclass(frozen=True, slots=True)
class Resultado:
    """Un resultado de búsqueda, ya normalizado."""

    titulo: str
    url: str
    resumen: str
    motor: str

    def como_dict(self) -> dict[str, str]:
        return {"titulo": self.titulo, "url": self.url, "resumen": self.resumen}


class Motor(Protocol):
    """Una forma de buscar. Intercambiable por configuración."""

    nombre: str

    async def buscar(self, consulta: str, *, maximo: int) -> list[Resultado]: ...


class MotorDdgs:
    """`ddgs`: metabuscador de librería, sin llaves y sin infraestructura.

    La librería es síncrona, así que la llamada va a un hilo: bloquear el event
    loop del scheduler durante una búsqueda dejaría parados los demás jobs.
    """

    def __init__(self, backend: str = "duckduckgo", *, region: str = "mx-es") -> None:
        self.nombre = f"ddgs:{backend}"
        self._backend = backend
        self._region = region

    async def buscar(self, consulta: str, *, maximo: int) -> list[Resultado]:
        try:
            crudos = await asyncio.to_thread(self._buscar_sincrono, consulta, maximo)
        except ImportError as exc:  # pragma: no cover — falta el extra `research`
            raise ErrorBusqueda(
                "falta la dependencia `ddgs`; instala el extra [research]",
                transitorio=False,
            ) from exc
        except Exception as exc:  # noqa: BLE001 — ddgs lanza de todo
            raise ErrorBusqueda(f"{self.nombre}: {type(exc).__name__}: {exc}") from exc

        resultados: list[Resultado] = []
        for crudo in crudos:
            url = str(crudo.get("href") or crudo.get("url") or "").strip()
            if not _es_http(url):
                continue
            resultados.append(
                Resultado(
                    titulo=str(crudo.get("title") or "").strip(),
                    url=url,
                    resumen=str(crudo.get("body") or crudo.get("snippet") or "").strip(),
                    motor=self.nombre,
                )
            )
        return resultados

    def _buscar_sincrono(self, consulta: str, maximo: int) -> list[dict[str, Any]]:
        from ddgs import DDGS

        with DDGS() as ddgs:
            return list(
                ddgs.text(
                    consulta,
                    region=self._region,
                    max_results=maximo,
                    backend=self._backend,
                )
            )


def _es_http(url: str) -> bool:
    partes = urlsplit(url)
    return partes.scheme in {"http", "https"} and bool(partes.netloc)


def _espera(intento: int, base: float, tope: float) -> float:
    return min(base * (2.0**intento), tope) * random.uniform(0.75, 1.25)  # noqa: S311


@dataclass(slots=True)
class _EstadoMotor:
    fallos: int = 0
    abierto: bool = False


def motores_por_defecto() -> list[Motor]:
    """La cadena declarada en `RESEARCH_MOTORES`, en orden."""
    nombres = [n.strip() for n in settings.research_motores.split(",") if n.strip()]
    return [MotorDdgs(nombre) for nombre in nombres]


class SearchExecutor:
    """Busca por la cadena de motores. Una instancia por corrida.

    El estado del circuito y el conjunto de URLs vistas viven en la instancia,
    así que se reinician solos en cada corrida — igual que hace el `Fetcher`.
    """

    def __init__(
        self,
        motores: list[Motor] | None = None,
        *,
        max_reintentos: int | None = None,
        umbral_circuito: int | None = None,
        espera_base_s: float = 2.0,
        espera_tope_s: float = 20.0,
    ) -> None:
        self._motores = motores if motores is not None else motores_por_defecto()
        self._max_reintentos = (
            max_reintentos if max_reintentos is not None else settings.research_max_reintentos
        )
        self._umbral = (
            umbral_circuito if umbral_circuito is not None else settings.fetch_umbral_circuito
        )
        self._base = espera_base_s
        self._tope = espera_tope_s
        self._estado: dict[str, _EstadoMotor] = {}
        self._urls: set[str] = set()
        self.consultas: list[str] = []

    @property
    def urls_permitidas(self) -> frozenset[str]:
        """Las URLs que **de verdad** devolvió una búsqueda.

        Es el conjunto contra el que se valida todo hallazgo del researcher.
        Si una URL no está aquí, nadie la vio: se descarta.
        """
        return frozenset(self._urls)

    @property
    def motores_en_circuito(self) -> list[str]:
        return sorted(n for n, e in self._estado.items() if e.abierto)

    async def buscar(self, consulta: str, *, maximo: int = 8) -> list[Resultado]:
        """Recorre la cadena hasta que un motor devuelva algo.

        Devuelve lista vacía si ninguno pudo: la corrida degrada y el
        researcher termina sin publicar, que es preferible a inventar.
        """
        consulta = consulta.strip()
        if not consulta:
            return []
        self.consultas.append(consulta)

        for motor in self._motores:
            estado = self._estado.setdefault(motor.nombre, _EstadoMotor())
            if estado.abierto:
                continue
            try:
                resultados = await self._con_reintentos(motor, consulta, maximo)
            except ErrorBusqueda as exc:
                estado.fallos += 1
                if estado.fallos >= self._umbral and not estado.abierto:
                    estado.abierto = True
                    log.warning(
                        "busqueda_circuito_abierto", motor=motor.nombre, fallos=estado.fallos
                    )
                log.warning("busqueda_fallida", motor=motor.nombre, error=str(exc)[:160])
                continue

            if resultados:
                self._urls.update(r.url for r in resultados)
                log.info(
                    "busqueda_ok",
                    motor=motor.nombre,
                    consulta=consulta[:120],
                    resultados=len(resultados),
                )
                return resultados
            # Vacío no es error: el motor contestó bien y no había nada. Se
            # avanza en la cadena sin tocar el circuito, igual que el fetcher.
            log.info("busqueda_vacia", motor=motor.nombre, consulta=consulta[:120])

        log.warning("busqueda_sin_resultados", consulta=consulta[:120])
        return []

    async def _con_reintentos(self, motor: Motor, consulta: str, maximo: int) -> list[Resultado]:
        ultimo: ErrorBusqueda | None = None
        for intento in range(1 + self._max_reintentos):
            try:
                return await motor.buscar(consulta, maximo=maximo)
            except ErrorBusqueda as exc:
                ultimo = exc
                if not exc.transitorio or intento == self._max_reintentos:
                    break
                await asyncio.sleep(_espera(intento, self._base, self._tope))
        assert ultimo is not None
        raise ultimo


@dataclass(slots=True)
class ReporteBusqueda:
    """Qué se buscó y con qué suerte. Va a las métricas de la corrida."""

    consultas: int = 0
    urls_vistas: int = 0
    motores_en_circuito: list[str] = field(default_factory=list)

    @classmethod
    def de(cls, ejecutor: SearchExecutor) -> ReporteBusqueda:
        return cls(
            consultas=len(ejecutor.consultas),
            urls_vistas=len(ejecutor.urls_permitidas),
            motores_en_circuito=ejecutor.motores_en_circuito,
        )


__all__ = [
    "ErrorBusqueda",
    "Motor",
    "MotorDdgs",
    "ReporteBusqueda",
    "Resultado",
    "SearchExecutor",
    "motores_por_defecto",
]
