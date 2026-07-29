"""Sacar el JSON de lo que contesta un modelo hablador.

Pedir `response_format=json_object` ayuda pero no garantiza nada: los modelos
siguen envolviendo la respuesta en fences de markdown, precediéndola de una
frase amable, o —los que razonan— dejando el contenido vacío y el JSON dentro
del canal de razonamiento. Nada de eso es un fallo del modelo que merezca tirar
la extracción entera; es un formato que hay que limpiar.

Lo que **no** se hace aquí: adivinar. Si tras limpiar no hay un objeto JSON
válido con las claves pedidas, se lanza `ErrorDeParseo` con el crudo dentro. Un
parser que rellena huecos convierte un error visible en un dato inventado.
"""

from __future__ import annotations

import json
import re
from typing import Any

from llm.providers.base import ErrorDeParseo

#: ```json … ``` o ``` … ```
_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$")
#: Bloques de razonamiento de algunos modelos abiertos.
_RAZONAMIENTO = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def limpiar(crudo: str) -> str:
    """Quita fences y bloques de razonamiento. No intenta parsear."""
    texto = _RAZONAMIENTO.sub("", crudo or "").strip()
    texto = _FENCE.sub("", texto).strip()
    return texto


def _primer_objeto(texto: str) -> str | None:
    """El primer objeto JSON balanceado del texto, o None.

    Se cuentan llaves en vez de usar una expresión regular porque el JSON tiene
    objetos anidados y ninguna expresión regular los cierra bien. Se ignoran las
    llaves dentro de cadenas, que es donde la cuenta ingenua se rompe.
    """
    inicio = texto.find("{")
    if inicio < 0:
        return None
    profundidad = 0
    en_cadena = False
    escapado = False
    for i in range(inicio, len(texto)):
        c = texto[i]
        if en_cadena:
            if escapado:
                escapado = False
            elif c == "\\":
                escapado = True
            elif c == '"':
                en_cadena = False
            continue
        if c == '"':
            en_cadena = True
        elif c == "{":
            profundidad += 1
        elif c == "}":
            profundidad -= 1
            if profundidad == 0:
                return texto[inicio : i + 1]
    return None


def parsear_json(
    contenido: str,
    *,
    claves_requeridas: tuple[str, ...] = (),
    respaldo: str | None = None,
) -> dict[str, Any]:
    """Objeto JSON del contenido, o `ErrorDeParseo`.

    `respaldo` es el canal de razonamiento: si el contenido viene vacío —lo que
    hace un razonador que gastó su presupuesto pensando— se busca ahí antes de
    darse por vencido.
    """
    for candidato in (contenido, respaldo):
        if not candidato:
            continue
        texto = limpiar(candidato)
        if not texto:
            continue
        bruto = _primer_objeto(texto)
        if bruto is None:
            continue
        try:
            datos = json.loads(bruto)
        except json.JSONDecodeError:
            continue
        if not isinstance(datos, dict):
            continue
        faltantes = [k for k in claves_requeridas if k not in datos]
        if faltantes:
            raise ErrorDeParseo(
                f"al JSON le faltan claves: {faltantes}",
                contenido_crudo=(contenido or "")[:2000],
            )
        return datos

    raise ErrorDeParseo(
        "no se encontró un objeto JSON en la respuesta",
        contenido_crudo=(contenido or "")[:2000],
    )


__all__ = ["limpiar", "parsear_json"]
