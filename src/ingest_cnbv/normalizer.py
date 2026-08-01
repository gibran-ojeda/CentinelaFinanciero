"""Del nombre regulatorio al del catálogo, y el reporte de lo que no casa.

El foundation manda no inventar (§3): la categoría de una institución es su
figura regulatoria vigente, no la percepción de mercado, y su nombre en los
boletines es el que la CNBV escribe, no el que le pongamos nosotros. Por eso
`instituciones.nombre_cnbv` se rellena leyendo el fichero real y lo que no se
puede confirmar **se reporta en vez de adivinarse**.

## Por qué no basta comparar cadenas

La CNBV llama a la misma institución de dos maneras **dentro del mismo libro**:
en el boletín de mayo de 2026, la hoja de cartera dice «Ualá» y «Openbank»
mientras la de capitalización dice «Banco Ualá» y «Openbank México». Casar
literalmente perdería el ICAP de esos dos bancos y nadie se enteraría: la
columna simplemente quedaría nula, que es un valor legítimo.

Así que se compara por una **clave normalizada** que quita acentos, la palabra
«banco» y el sufijo de país. Es una regla acotada y con red: si dos
instituciones **del catálogo** colapsan a la misma clave, eso sí es un error y
se lanza — sería mezclar los indicadores de dos entidades distintas.

## Lo que se reporta

Dos listas, y las dos importan por razones distintas:

- **Del catálogo sin datos**: es el hueco que deja a una institución sin
  banderas de salud. Va a `job_runs` y se mira.
- **Del boletín sin catálogo**: son decenas —la CNBV publica todo el sector— y
  sólo se cuentan. Son las candidatas a ampliar el catálogo, no un fallo.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from core.logging import get_logger
from ingest_cnbv.parser import FilaInstitucion

log = get_logger(__name__)

#: Palabras que la CNBV pone y quita entre publicaciones sin cambiar de quién
#: habla. Se recortan sólo cuando sobra algo después: «Banco Base» no puede
#: quedar en «base» a secas si con eso se vaciara el nombre.
_DECORACIONES = (
    r"^banco\s+",
    r"\s+mexico$",
    r"\s+m[eé]xico$",
    r",?\s+s\.?a\.?( de c\.?v\.?)?.*$",
    r",?\s+s\.?f\.?p\.?.*$",
    r"\s+servicios financieros$",
)


class MapeoAmbiguo(Exception):
    """Dos instituciones del catálogo comparten clave. Mezclarlas sería peor."""


def clave(nombre: str) -> str:
    """Forma canónica de un nombre, para comparar catálogo con boletín."""
    crudo = str(nombre or "").strip().lower()
    sin_acentos = "".join(
        c for c in unicodedata.normalize("NFD", crudo) if unicodedata.category(c) != "Mn"
    )
    texto = re.sub(r"\s+", " ", sin_acentos).strip()
    for patron in _DECORACIONES:
        recortado = re.sub(patron, "", texto).strip()
        if recortado:
            texto = recortado
    return re.sub(r"[^a-z0-9]+", "", texto)


@dataclass(slots=True)
class ReporteMapeo:
    """Qué casó y qué no."""

    #: `institucion_id` → fila combinada del boletín.
    casadas: dict[int, FilaInstitucion] = field(default_factory=dict)
    #: Nombres del catálogo sin `nombre_cnbv` puesto.
    sin_mapear: list[str] = field(default_factory=list)
    #: Con `nombre_cnbv` puesto pero que el boletín no trae este periodo.
    sin_datos: list[str] = field(default_factory=list)
    #: Cuántas filas del boletín no corresponden a ninguna del catálogo.
    fuera_del_catalogo: int = 0

    def como_metricas(self) -> dict[str, Any]:
        return {
            "instituciones_casadas": len(self.casadas),
            "sin_nombre_cnbv": self.sin_mapear,
            "sin_datos_en_el_boletin": self.sin_datos,
            "filas_fuera_del_catalogo": self.fuera_del_catalogo,
        }

    def render(self) -> str:
        lineas = [
            f"  casadas                 {len(self.casadas):>4}",
            f"  fuera del catálogo      {self.fuera_del_catalogo:>4}",
        ]
        if self.sin_mapear:
            lineas.append(f"  sin nombre_cnbv         {', '.join(self.sin_mapear)}")
        if self.sin_datos:
            lineas.append(f"  sin datos este periodo  {', '.join(self.sin_datos)}")
        return "\n".join(lineas)


@dataclass(frozen=True, slots=True)
class Candidata:
    """Una institución del catálogo lista para casarse con el boletín."""

    id: int
    nombre: str
    nombre_cnbv: str | None


def mapear(candidatas: list[Candidata], filas: dict[str, FilaInstitucion]) -> ReporteMapeo:
    """Cruza el catálogo con lo leído del boletín.

    Args:
        filas: lo que devuelve `parser.combinar`, indexado por el nombre tal
            cual lo escribe la CNBV.
    """
    reporte = ReporteMapeo()

    # Varias filas del boletín pueden caer en la misma clave —«Ualá» y «Banco
    # Ualá»— y hay que fundirlas antes de cruzar, o la segunda pisaría a la
    # primera y se perdería justo la columna que sólo trae una de las dos.
    por_clave: dict[str, FilaInstitucion] = {}
    for nombre, fila in filas.items():
        llave = clave(nombre)
        if not llave:
            continue
        acumulada = por_clave.setdefault(llave, FilaInstitucion(nombre_cnbv=nombre))
        for campo, valor in fila.valores.items():
            # No se pisa un dato con un hueco: la hoja que no publica ese
            # concepto trae `None` y la que sí lo trae puede venir después.
            if valor is not None or campo not in acumulada.valores:
                acumulada.valores[campo] = valor

    del_catalogo: dict[str, Candidata] = {}
    for candidata in candidatas:
        if not candidata.nombre_cnbv:
            reporte.sin_mapear.append(candidata.nombre)
            continue
        llave = clave(candidata.nombre_cnbv)
        chocada = del_catalogo.get(llave)
        if chocada is not None:
            raise MapeoAmbiguo(
                f"'{candidata.nombre}' y '{chocada.nombre}' se reducen a la misma "
                f"clave '{llave}'. Mezclar sus indicadores sería peor que no cargarlos."
            )
        del_catalogo[llave] = candidata

    usadas: set[str] = set()
    for llave, candidata in del_catalogo.items():
        encontrada = por_clave.get(llave)
        if encontrada is None:
            reporte.sin_datos.append(candidata.nombre)
            log.info(
                "cnbv_sin_datos",
                institucion=candidata.nombre,
                nombre_cnbv=candidata.nombre_cnbv,
            )
            continue
        usadas.add(llave)
        reporte.casadas[candidata.id] = encontrada

    reporte.fuera_del_catalogo = len(por_clave) - len(usadas)

    log.info("cnbv_mapeo", **reporte.como_metricas())
    return reporte


__all__ = ["Candidata", "MapeoAmbiguo", "ReporteMapeo", "clave", "mapear"]
