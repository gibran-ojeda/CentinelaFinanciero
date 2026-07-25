"""Tests del contrato de la API.

Verifican las obligaciones que el contrato hace cumplir por tipo, no por
disciplina de quien lo implemente.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from api.schemas import (
    DISCLAIMER,
    AltaTasa,
    Procedencia,
    RespuestaCalculadora,
    RespuestaComparador,
    SolicitudCalculadora,
)


def test_provenance_requires_a_date_and_a_source() -> None:
    """§11 y §19: ninguna tasa puede publicarse sin decir de dónde salió."""
    with pytest.raises(ValidationError):
        Procedencia()  # type: ignore[call-arg]

    with pytest.raises(ValidationError):
        Procedencia(fuente="MANUAL")  # type: ignore[call-arg]


def test_source_url_is_the_only_optional_part_of_provenance() -> None:
    """Banxico se identifica por serie; no toda fuente tiene URL de página."""
    procedencia = Procedencia(
        fecha_dato="2026-07-23",  # type: ignore[arg-type]
        fuente="MANUAL",  # type: ignore[arg-type]
        estado="VIGENTE",  # type: ignore[arg-type]
        verificada=True,
    )
    assert procedencia.fuente_url is None


def test_provenance_requires_saying_whether_the_rate_is_verified() -> None:
    """No hay default: publicar una tasa obliga a declarar si está confirmada.

    Un default de `True` haría que olvidar el campo afirmara algo falso, y uno
    de `False` marcaría como dudosos los datos buenos. Ninguno de los dos es
    aceptable, así que el campo es obligatorio.
    """
    with pytest.raises(ValidationError):
        Procedencia(  # type: ignore[call-arg]
            fecha_dato="2026-07-23",  # type: ignore[arg-type]
            fuente="MANUAL",  # type: ignore[arg-type]
            estado="VIGENTE",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("respuesta", [RespuestaComparador, RespuestaCalculadora])
def test_responses_carry_the_disclaimer_by_default(respuesta: type) -> None:
    """§19: la API declara que no es asesor financiero en cada respuesta."""
    assert respuesta.model_fields["disclaimer"].default == DISCLAIMER
    assert "no es asesor financiero" in DISCLAIMER


def test_calculator_rejects_a_non_positive_amount() -> None:
    for monto in ("0", "-100"):
        with pytest.raises(ValidationError):
            SolicitudCalculadora(monto=Decimal(monto), producto_ids=[1])


def test_calculator_requires_at_least_one_product() -> None:
    with pytest.raises(ValidationError):
        SolicitudCalculadora(monto=Decimal("100000"), producto_ids=[])


def test_calculator_caps_the_number_of_products() -> None:
    """Un límite explícito evita que una petición barra el catálogo entero."""
    with pytest.raises(ValidationError):
        SolicitudCalculadora(monto=Decimal("100000"), producto_ids=list(range(21)))


def test_calculator_term_is_optional_and_positive() -> None:
    sin_plazo = SolicitudCalculadora(monto=Decimal("100000"), producto_ids=[1])
    assert sin_plazo.plazo_dias is None

    with pytest.raises(ValidationError):
        SolicitudCalculadora(monto=Decimal("100000"), producto_ids=[1], plazo_dias=0)


def test_admin_rate_entry_rejects_implausible_values() -> None:
    """La misma defensa que la CLI: un 950 no puede entrar por la API."""
    with pytest.raises(ValidationError):
        AltaTasa(producto_id=1, tasa_nominal=Decimal("950"), fecha_dato="2026-07-23")  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        AltaTasa(producto_id=1, tasa_nominal=Decimal("-1"), fecha_dato="2026-07-23")  # type: ignore[arg-type]


def test_admin_rate_entry_defaults_to_manual() -> None:
    alta = AltaTasa(producto_id=1, tasa_nominal=Decimal("6.18"), fecha_dato="2026-07-23")  # type: ignore[arg-type]
    assert alta.fuente == "MANUAL"


def test_comparator_response_echoes_its_calculation_context() -> None:
    """El resultado tiene que poder auditarse sin volver a consultar la base."""
    campos = set(RespuestaComparador.model_fields)
    assert {"inflacion_anual", "valor_udi", "tasa_retencion_capital"} <= campos
