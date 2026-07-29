"""Agente de tasas: leer la página de cada institución y proponer qué publicar.

El reparto de responsabilidades es el de §15 del foundation, y no se mezcla:

- `fetcher` — **determinista**. Obtiene la página. Es la parte que se rompe por
  red, por WAF o por JavaScript, y por eso lleva toda la resiliencia.
- `extractor` — **el LLM**. Convierte el texto en una tasa estructurada. Es la
  parte que cambia de forma con cada rediseño, y por eso no la hace un selector.
- `reviewer` — **determinista otra vez**. Decide si eso se publica o pasa por
  una persona. Ninguna decisión sobre publicar la toma un modelo.
"""
