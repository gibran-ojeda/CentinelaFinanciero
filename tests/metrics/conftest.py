"""Fixtures compartidas del motor de métricas.

Los dos ejercicios fiscales están disponibles como fixtures porque los ejemplos
obligatorios del foundation se escribieron con la tasa de 2025 (0.50%) y el
sistema opera con la de 2026 (0.90%). Poder pedir cualquiera de las dos hace
explícito qué parámetro reproduce qué número.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from domain.models import ParametrosFiscales


@pytest.fixture
def fiscal_2026() -> ParametrosFiscales:
    """Vigente: artículo 24 de la LIF 2026."""
    return ParametrosFiscales(
        anio=2026,
        tasa_retencion_capital=Decimal("0.90"),
        vigente_desde=date(2026, 1, 1),
        fuente_url="http://www.diputados.gob.mx/LeyesBiblio/pdf/LIF_2026.pdf",
    )


@pytest.fixture
def fiscal_2025() -> ParametrosFiscales:
    """El ejercicio con el que se escribieron los ejemplos del foundation."""
    return ParametrosFiscales(
        anio=2025,
        tasa_retencion_capital=Decimal("0.50"),
        vigente_desde=date(2025, 1, 1),
    )
