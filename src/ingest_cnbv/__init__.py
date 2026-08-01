"""Ingesta del Portafolio de Información de la CNBV (fase 8, nivel 1).

De aquí salen los indicadores de salud institucional de §5.1 —IMOR, ICAP,
NICAP, cobertura de cartera, captación— con los que se recomputan las banderas.
Determinista y sin LLM: son cifras regulatorias publicadas, no interpretadas.

La CNBV publica con uno a tres meses de rezago y sin fecha fija, así que el job
no espera un archivo concreto un día concreto: pregunta qué hay y compara con
lo que ya cargó.
"""

from __future__ import annotations

__all__: list[str] = []
