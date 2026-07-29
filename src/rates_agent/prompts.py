"""Carga de las plantillas de `prompts/`.

Fuera del código a propósito: un prompt se itera leyéndolo y comparando
versiones, y eso es incómodo cuando vive escapado dentro de un `.py`. Se cachean
en memoria porque un job que procesa dieciocho páginas no tiene por qué leer el
mismo archivo dieciocho veces.

Se renderizan con `format_map` y no con f-strings: la plantilla trae ejemplos de
JSON llenos de llaves, y `format` sobre ellos reventaría. `format_map` con un
diccionario que sólo tiene las claves esperadas falla igual con `{`, así que las
llaves literales de los ejemplos van dobladas en el archivo — que es la
convención de `str.format` y queda documentada aquí para que nadie la deshaga.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


def _raiz() -> Path:
    """Dónde está `prompts/`: primero el cwd, luego relativo al paquete.

    El mismo orden que `cli.seed` y `core.schema_check`, y por el mismo motivo:
    instalado, este módulo vive en `site-packages` y subir dos niveles apunta
    fuera del proyecto. En el contenedor el `WORKDIR` es `/app`.
    """
    cwd = Path.cwd() / "prompts"
    if cwd.is_dir():
        return cwd
    return Path(__file__).resolve().parents[2] / "prompts"


@lru_cache(maxsize=16)
def plantilla(nombre: str) -> str:
    """Contenido de `prompts/<nombre>.md`."""
    ruta = _raiz() / f"{nombre}.md"
    if not ruta.is_file():
        raise FileNotFoundError(f"no existe la plantilla {ruta}")
    return ruta.read_text(encoding="utf-8")


def render(nombre: str, **valores: object) -> str:
    """Plantilla con sus huecos rellenos."""
    return plantilla(nombre).format_map({k: str(v) for k, v in valores.items()})


__all__ = ["plantilla", "render"]
