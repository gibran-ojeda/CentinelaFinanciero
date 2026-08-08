"""Ingesta del SIE de Banxico (nivel 1 de §15 del foundation).

Es la fuente oficial y determinista: no hay LLM, no hay scraping y no hay cola
de revisión. Lo que Banxico publica se guarda tal cual, y de ahí salen la UDI
que convierte los límites de cobertura a pesos, la inflación con la que se
calcula la ganancia real, y las tasas de CETES del comparador.
"""

from __future__ import annotations

__all__: list[str] = []
