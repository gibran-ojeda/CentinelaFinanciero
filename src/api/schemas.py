"""Contrato de la API. Se define antes que la implementación.

Este módulo es el contrato del BFF de Astro (fase 5) y del servidor MCP
(fase 10). Lo que no esté aquí no existe para el frontend.

Dos reglas que el tipo hace cumplir, y que no son estilísticas:

1. **Ninguna tasa viaja sin `fecha_dato` ni `fuente`.** §11 (actualización
   transparente) y §19 (auditabilidad) lo exigen. Son campos obligatorios, no
   opcionales, precisamente para que nadie pueda olvidarlos.
2. **La GAT viaja con su origen.** El usuario tiene derecho a saber si mira un
   dato regulado o una estimación nuestra (§4.4).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from domain.enums import (
    CategoriaInstitucion,
    FuenteTasa,
    Liquidez,
    NivelCapitalizacion,
    Severidad,
    TipoBandera,
    TipoInstrumento,
    TipoProducto,
    TipoSeguro,
)

DISCLAIMER = (
    "Centinela Financiero no es asesor financiero ni intermediario. La información "
    "se publica con fines comparativos, proviene de fuentes públicas y puede "
    "contener errores o estar desactualizada. Verifica siempre las condiciones "
    "directamente con la institución antes de invertir."
)


class Esquema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ─── Piezas reutilizables ─────────────────────────────────────


class Procedencia(Esquema):
    """De dónde salió un dato y cuándo. Acompaña a toda tasa."""

    fecha_dato: date = Field(description="Fecha a la que corresponde la observación")
    fuente: FuenteTasa
    fuente_url: str | None = None


class GatSchema(Esquema):
    nominal: Decimal
    real: Decimal
    origen: Literal["PUBLICADA", "CALCULADA"]
    es_calculada: bool = Field(
        description="Si es true, la institución no la publica y es una estimación nuestra"
    )


class CoberturaSchema(Esquema):
    tipo: TipoSeguro
    limite_udis: Decimal | None
    limite_mxn: Decimal | None = Field(
        description="null significa sin límite (deuda soberana), no desconocido"
    )
    valor_udi: Decimal
    sin_limite: bool
    sin_cobertura: bool


class BanderaSchema(Esquema):
    tipo: TipoBandera
    severidad: Severidad
    motivo: str
    periodo_dato: date | None = Field(
        description="Periodo del dato que la originó. La CNBV publica con rezago."
    )
    compuesta: bool


class InstitucionResumen(Esquema):
    id: int
    nombre: str
    slug: str
    categoria: CategoriaInstitucion
    tipo_seguro: TipoSeguro


# ─── Comparador ───────────────────────────────────────────────


class FilaComparador(Esquema):
    """Una fila de la tabla principal (§7)."""

    institucion: InstitucionResumen
    producto_id: int
    producto: str
    producto_slug: str
    tipo: TipoProducto
    instrumento: TipoInstrumento
    plazo_dias: int | None
    monto_minimo: Decimal
    liquidez: Liquidez

    tasa_nominal: Decimal
    ten: Decimal = Field(description="Tasa efectiva neta anual, después de ISR")
    gat: GatSchema
    cobertura: CoberturaSchema
    banderas: list[BanderaSchema]
    procedencia: Procedencia


class RespuestaComparador(Esquema):
    filas: list[FilaComparador]
    total: int
    #: Contexto con el que se calcularon las métricas. Va en la respuesta para
    #: que el resultado sea reproducible y auditable sin consultar la base.
    inflacion_anual: Decimal
    valor_udi: Decimal
    tasa_retencion_capital: Decimal
    generado_en: datetime
    disclaimer: str = DISCLAIMER


# ─── Detalle de institución ───────────────────────────────────


class IndicadoresSchema(Esquema):
    periodo: date
    imor: Decimal | None
    icap: Decimal | None
    icor: Decimal | None
    nicap_nivel: NivelCapitalizacion | None
    captacion: Decimal | None
    cartera_total: Decimal | None
    fuente_url: str | None


class ProductoDetalle(Esquema):
    id: int
    nombre: str
    slug: str
    tipo: TipoProducto
    instrumento: TipoInstrumento
    plazo_dias: int | None
    monto_minimo: Decimal
    liquidez: Liquidez
    penalizacion_retiro: str | None
    tasa_nominal: Decimal | None
    ten: Decimal | None
    gat: GatSchema | None
    procedencia: Procedencia | None = Field(
        description="null si el producto no tiene todavía una tasa publicable"
    )


class DetalleInstitucion(Esquema):
    """Todas las capas de profundidad de §11: la UI decide qué mostrar."""

    id: int
    nombre: str
    slug: str
    categoria: CategoriaInstitucion
    tipo_seguro: TipoSeguro
    estatus_regulatorio: str | None
    url_sitio: str | None
    activa: bool
    notas: str | None

    cobertura: CoberturaSchema
    productos: list[ProductoDetalle]
    indicadores_ultimo_periodo: IndicadoresSchema | None
    banderas_activas: list[BanderaSchema]
    banderas_historicas: list[BanderaSchema]
    disclaimer: str = DISCLAIMER


# ─── Calculadora ──────────────────────────────────────────────


class SolicitudCalculadora(Esquema):
    monto: Decimal = Field(gt=0, description="Monto a invertir en MXN")
    plazo_dias: int | None = Field(
        default=None,
        gt=0,
        description="Horizonte en días. Si se omite, se usa el plazo de cada producto.",
    )
    producto_ids: list[int] = Field(min_length=1, max_length=20)
    inflacion_anual: Decimal | None = Field(
        default=None,
        description="Permite simular otro escenario. Por defecto, el INPC vigente.",
    )


class CascadaSchema(Esquema):
    """Los cinco conceptos de §6, en orden de presentación."""

    monto_invertido: Decimal
    rendimiento_bruto: Decimal
    isr_retenido: Decimal
    rendimiento_neto: Decimal
    efecto_inflacion: Decimal
    ganancia_real: Decimal

    plazo_dias: int
    tasa_nominal: Decimal
    ten: Decimal
    inflacion_anual: Decimal
    nota_fiscal: str


class ResultadoCalculadora(Esquema):
    institucion: InstitucionResumen
    producto_id: int
    producto: str
    cascada: CascadaSchema
    cobertura: CoberturaSchema
    monto_expuesto: Decimal = Field(
        description="Parte del monto que quedaría sin protección si la institución quiebra"
    )
    banderas: list[BanderaSchema]
    procedencia: Procedencia


class RespuestaCalculadora(Esquema):
    resultados: list[ResultadoCalculadora]
    generado_en: datetime
    disclaimer: str = DISCLAIMER


# ─── Meta ─────────────────────────────────────────────────────


class FrescuraFuente(Esquema):
    fuente: str
    ultima_actualizacion: date | None
    dias_desde_actualizacion: int | None
    sla_dias: int
    dentro_de_sla: bool
    observaciones: int


class RespuestaFrescura(Esquema):
    """Obligación de §11: la fecha de cada dato, siempre visible."""

    fuentes: list[FrescuraFuente]
    generado_en: datetime
    todo_dentro_de_sla: bool


# ─── Admin ────────────────────────────────────────────────────


class AltaTasa(Esquema):
    producto_id: int
    tasa_nominal: Decimal = Field(ge=0, le=100)
    gat_nominal: Decimal | None = Field(default=None, ge=0, le=100)
    gat_real: Decimal | None = None
    fecha_dato: date
    fuente: FuenteTasa = FuenteTasa.MANUAL
    fuente_url: str | None = None
    notas: str | None = None


class TasaCreada(Esquema):
    id: int
    producto_id: int
    tasa_nominal: Decimal
    fecha_dato: date
    estado: str


class RevisionPendiente(Esquema):
    id: int
    tasa_id: int
    producto: str
    institucion: str
    motivo: str
    valor_anterior: Decimal | None
    valor_nuevo: Decimal
    estado: str
    created_at: datetime


class ResolucionRevision(Esquema):
    aprobar: bool
    revisor: str = Field(min_length=1, max_length=80)
    comentario: str | None = None


__all__ = [
    "DISCLAIMER",
    "AltaTasa",
    "BanderaSchema",
    "CascadaSchema",
    "CoberturaSchema",
    "DetalleInstitucion",
    "FilaComparador",
    "FrescuraFuente",
    "GatSchema",
    "IndicadoresSchema",
    "InstitucionResumen",
    "Procedencia",
    "ProductoDetalle",
    "ResolucionRevision",
    "RespuestaCalculadora",
    "RespuestaComparador",
    "RespuestaFrescura",
    "ResultadoCalculadora",
    "RevisionPendiente",
    "SolicitudCalculadora",
    "TasaCreada",
]
