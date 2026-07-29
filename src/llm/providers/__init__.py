"""Proveedores de LLM. Hoy uno: cualquier API compatible con OpenAI."""

from llm.providers.base import (
    ErrorDeParseo,
    ErrorLimiteDePeticiones,
    ErrorPresupuestoAgotado,
    ErrorProveedor,
    ErrorTiempoAgotado,
    ProveedorLLM,
    RespuestaLLM,
)
from llm.providers.openai_compat import PRECIOS, ProveedorOpenAICompat, costo_usd

__all__ = [
    "PRECIOS",
    "ErrorDeParseo",
    "ErrorLimiteDePeticiones",
    "ErrorPresupuestoAgotado",
    "ErrorProveedor",
    "ErrorTiempoAgotado",
    "ProveedorLLM",
    "ProveedorOpenAICompat",
    "RespuestaLLM",
    "costo_usd",
]
