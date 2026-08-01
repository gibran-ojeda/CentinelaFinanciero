"""El NICAP de las SOFIPOs, que sólo existe en PDF.

`NivelCapitalizacion` (N1–N4) es la categoría prudencial que el Comité de
Supervisión Auxiliar asigna a cada sociedad según su nivel de capitalización, y
es una de las banderas de §5.1. La CNBV la publica **únicamente** en el PDF
mensual de «Nivel de Capitalización y Alertas Tempranas»: no está en el boletín
estadístico ni se deduce de ningún otro número que sí lo esté.

El PDF trae texto de verdad —no es un escaneo—, una página, y una línea por
sociedad::

    7 027045 KU-BO 184,885,173 65,934,537 280% 1 Atlántico Pacífico
    11 027033 Libertad5/ - - n.d. n.d. Fine servicios

Es decir: consecutivo, clave CASFIM, nombre, capital neto, requerimientos,
NICAP en porcentaje, **categoría** y federación. La categoría es lo que se
guarda; el resto se lee para validar que la línea es lo que parece.

Tres cosas del formato que hay que tragar y no son negociables:

- **Los números salen con espacios de más.** `8 4,869,859` es 84,869,859: es
  el interletrado del PDF, no un separador. Como sólo se necesita la
  categoría, no se pelea con ellos — se exige que la forma general encaje.
- **Los nombres arrastran su nota al pie.** `Crediclub3/`, `Libertad5/`. Se
  recortan, porque el nombre es la clave de mapeo.
- **`n.d.` no es cero ni es N4.** Significa que ese NICAP está en revisión de
  la propia CNBV, que es un estado distinto de «mal capitalizada» y no puede
  acabar pintando una bandera. Se guarda como ausencia.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from core.logging import get_logger
from ingest_cnbv.parser import FormatoInesperado

log = get_logger(__name__)

#: Encabezado que tiene que estar. Si no, no es este documento.
_TITULO = "clasificacion de capitalizacion de las sociedades financieras populares"

#: «CIFRAS AL 31 DE MAYO DE 2026». Es la comprobación de periodo que importa:
#: verifica el contenido, no el nombre del archivo.
_CIFRAS_AL = re.compile(r"cifras al\s+(\d{1,2})\s+de\s+([a-z]+)\s+de\s+(\d{4})")

#: Una línea de sociedad. El nombre es perezoso para que no se coma los
#: números, y los importes admiten los espacios que mete el PDF.
_LINEA = re.compile(
    r"^\s*\d{1,3}\s+"
    r"(?P<clave>\d{6})\s+"
    r"(?P<nombre>\S.*?)\s+"
    r"(?:-|[\d][\d,\s]*)\s+"
    r"(?:-|[\d][\d,\s]*)\s+"
    r"(?P<nicap>n\.d\.|[\d][\d,\s]*%)\s+"
    r"(?P<categoria>n\.d\.|[1-4])(?:\s|$)"
)

#: `Crediclub3/` → `Crediclub`.
_NOTA = re.compile(r"\d+/\s*$")

MESES_ES: dict[str, int] = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}


@dataclass(frozen=True, slots=True)
class NivelLeido:
    """Lo que el PDF dice de una sociedad."""

    nombre_cnbv: str
    clave_casfim: str
    #: `N1`–`N4`, o `None` si la CNBV tiene el dato en revisión (`n.d.`).
    nivel: str | None
    #: Capital neto sobre requerimientos, en porcentaje. Informativo.
    porcentaje: Decimal | None


def _sin_acentos(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn"
    )


def _porcentaje(crudo: str) -> Decimal | None:
    limpio = crudo.replace(" ", "").replace(",", "").rstrip("%")
    try:
        return Decimal(limpio)
    except InvalidOperation:
        return None


def leer_nicap(ruta: Path, *, periodo: date) -> list[NivelLeido]:
    """Lee el PDF de capitalización. Lanza `FormatoInesperado` si no cuadra."""
    import pdfplumber

    try:
        with pdfplumber.open(ruta) as pdf:
            texto = "\n".join(pagina.extract_text() or "" for pagina in pdf.pages)
    except FormatoInesperado:
        raise
    except Exception as exc:  # noqa: BLE001 — pdfplumber lanza de todo
        raise FormatoInesperado(
            f"{ruta.name}: no se pudo abrir como PDF ({type(exc).__name__})"
        ) from exc

    plano = _sin_acentos(texto)
    if _TITULO not in plano:
        raise FormatoInesperado(
            f"{ruta.name}: no parece el documento de capitalización de SOFIPOs. "
            f"Se esperaba el título '{_TITULO}'."
        )
    if not texto.strip():
        raise FormatoInesperado(
            f"{ruta.name}: el PDF no tiene texto extraíble. Si la CNBV pasó a "
            f"publicarlo escaneado, esto necesita OCR y no un parser."
        )

    _validar_periodo(plano, periodo, ruta)

    niveles: list[NivelLeido] = []
    for linea in texto.splitlines():
        coincidencia = _LINEA.match(linea)
        if coincidencia is None:
            continue
        nombre = _NOTA.sub("", coincidencia.group("nombre")).strip()
        if not nombre or _sin_acentos(nombre).startswith("total"):
            continue
        categoria = coincidencia.group("categoria")
        niveles.append(
            NivelLeido(
                nombre_cnbv=nombre,
                clave_casfim=coincidencia.group("clave"),
                # `n.d.` es «en revisión por la CNBV», no «mal capitalizada».
                # Traducirlo a N4 pintaría una bandera roja por un trámite.
                nivel=None if categoria == "n.d." else f"N{categoria}",
                porcentaje=_porcentaje(coincidencia.group("nicap")),
            )
        )

    if not niveles:
        raise FormatoInesperado(
            f"{ruta.name}: el título está pero no se reconoció ninguna línea de "
            f"sociedad. El formato de la tabla cambió."
        )

    log.info(
        "cnbv_nicap_leido",
        archivo=ruta.name,
        sociedades=len(niveles),
        en_revision=sum(1 for n in niveles if n.nivel is None),
        periodo=periodo.isoformat(),
    )
    return niveles


def _validar_periodo(plano: str, periodo: date, ruta: Path) -> None:
    """El documento tiene que decir que es del periodo que se está cargando.

    Se comprueba contra el texto y no contra el nombre del archivo: el nombre
    lo pone quien sube el fichero y el encabezado lo genera el reporte.
    """
    coincidencia = _CIFRAS_AL.search(plano)
    if coincidencia is None:
        raise FormatoInesperado(
            f"{ruta.name}: no se encontró la línea «CIFRAS AL ... DE ... DE ...» "
            f"que dice a qué periodo corresponde."
        )
    mes = MESES_ES.get(coincidencia.group(2))
    anio = int(coincidencia.group(3))
    if mes is None:
        raise FormatoInesperado(f"{ruta.name}: mes no reconocido '{coincidencia.group(2)}'")
    if (anio, mes) != (periodo.year, periodo.month):
        raise FormatoInesperado(
            f"{ruta.name}: el documento dice ser de {anio}-{mes:02d} y se está "
            f"cargando como {periodo.year}-{periodo.month:02d}."
        )


__all__ = ["MESES_ES", "NivelLeido", "leer_nicap"]
