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
    EstadoIndicador,
    EstadoTasa,
    FuenteTasa,
    Liquidez,
    NivelCapitalizacion,
    Severidad,
    TipoBandera,
    TipoInstrumento,
    TipoProducto,
    TipoSeguro,
    UnidadIndicador,
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
    """De dónde salió un dato, cuándo, y si está confirmado."""

    fecha_dato: date = Field(description="Fecha a la que corresponde la observación")
    fuente: FuenteTasa
    fuente_url: str | None = None
    estado: EstadoTasa
    verificada: bool = Field(
        description=(
            "false significa que la tasa no se pudo confirmar contra la fuente oficial "
            "de la institución. La UI está obligada a mostrarla como tal, nunca como "
            "un dato confirmado."
        )
    )


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


class TramoSchema(Esquema):
    """Un escalón de una tasa escalonada por saldo: `[desde, hasta)`."""

    desde: Decimal
    hasta: Decimal | None = Field(description="null = sin techo publicado (infinito)")
    tasa_nominal: Decimal


class InstitucionResumen(Esquema):
    id: int
    nombre: str
    slug: str
    categoria: CategoriaInstitucion
    tipo_seguro: TipoSeguro
    es_demostracion: bool = Field(
        default=False,
        description=(
            "La institución es **ficticia**, sembrada para ilustrar el comportamiento "
            "del producto. Distinto de una tasa sin verificar, que sí es de una "
            "institución real: eso viaja en `procedencia.verificada`."
        ),
    )


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

    tasa_nominal: Decimal = Field(
        description="En un producto escalonado, la tasa del primer tramo — la titular"
    )
    ten: Decimal = Field(description="Tasa efectiva neta anual, después de ISR")
    gat: GatSchema
    cobertura: CoberturaSchema
    banderas: list[BanderaSchema]
    procedencia: Procedencia

    condiciones: str | None = Field(
        default=None,
        description=(
            "La letra pequeña de esta observación: promociones, membresías, "
            "requisitos de uso. Va en la fila porque una tasa condicionada sin "
            "su condición al lado se lee como incondicional."
        ),
    )

    escalonada: bool = False
    tramos: list[TramoSchema] = Field(
        default_factory=list,
        description="Escalera por saldo; vacía = la tasa aplica a todo el saldo",
    )
    tasa_efectiva: Decimal | None = Field(
        default=None,
        description=(
            "Nominal ponderada al monto consultado. Viaja en todas las filas cuando "
            "hay monto (en las planas coincide con la titular); null sin monto."
        ),
    )
    ten_efectiva: Decimal | None = Field(
        default=None, description="TEN de la ponderada; null si no se consultó monto"
    )


class RespuestaComparador(Esquema):
    filas: list[FilaComparador]
    total: int
    #: Contexto con el que se calcularon las métricas. Va en la respuesta para
    #: que el resultado sea reproducible y auditable sin consultar la base.
    inflacion_anual: Decimal
    valor_udi: Decimal
    tasa_retencion_capital: Decimal
    monto_consultado: Decimal | None = Field(
        default=None,
        description="El monto con el que se calcularon las tasas efectivas, si hubo",
    )
    generado_en: datetime
    disclaimer: str = DISCLAIMER


# ─── Detalle de institución ───────────────────────────────────


class IndicadorEvaluadoSchema(Esquema):
    """Un indicador con su semáforo, listo para pintar como tarjeta.

    El estado sale de la **misma** regla que emite la bandera, así que la
    tarjeta y la bandera no pueden contradecirse. El formato del número es
    cosa de la UI; aquí viaja el valor y su unidad.
    """

    clave: str
    etiqueta: str
    valor: Decimal | None
    valor_texto: str | None = Field(
        description='Para los que no son número, como el nivel NICAP ("N2")'
    )
    unidad: UnidadIndicador
    estado: EstadoIndicador
    descripcion: str


class IndicadoresSchema(Esquema):
    periodo: date
    imor: Decimal | None
    icap: Decimal | None
    icor: Decimal | None
    nicap_nivel: NivelCapitalizacion | None
    captacion: Decimal | None
    cartera_total: Decimal | None
    fuente_url: str | None
    evaluados: list[IndicadorEvaluadoSchema] = Field(
        default_factory=list,
        description="Los mismos indicadores con su estado respecto a los umbrales vigentes",
    )


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
    escalonada: bool = False
    tramos: list[TramoSchema] = Field(default_factory=list)


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
    es_demostracion: bool = False
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
    """Los cinco conceptos de §6, en orden de presentación.

    `tasa_nominal` es la tasa **aplicada** al monto: en un producto escalonado
    es la ponderada de su escalera, no la titular. El nombre se conserva por
    estabilidad del contrato.
    """

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
    escalonada: bool = False
    tramos: list[TramoSchema] = Field(default_factory=list)


class RespuestaCalculadora(Esquema):
    resultados: list[ResultadoCalculadora]
    generado_en: datetime
    disclaimer: str = DISCLAIMER


# ─── Calculadora de combinación ───────────────────────────────

#: Se dice en la respuesta, no sólo en la página de metodología: quien consuma
#: la API por su cuenta tiene que recibir la misma advertencia que el usuario.
AVISO_OPTIMIZADOR = (
    "El reparto propuesto es una heurística informativa: cada peso va al tramo con "
    "mejor tasa efectiva neta, hasta el límite de seguro de depósito de cada "
    "institución y respetando el monto mínimo de cada producto. No es una "
    "recomendación de inversión ni considera tu situación fiscal, tu liquidez ni tu "
    "tolerancia al riesgo."
)


class ItemCombinacion(Esquema):
    producto_id: int
    porcentaje: Decimal = Field(
        ge=0,
        le=100,
        description="Se normaliza para que el total sume 100: expresa una proporción",
    )


class SolicitudCombinacion(Esquema):
    monto_total: Decimal = Field(gt=0)
    horizonte_dias: int = Field(gt=0, le=3650)
    items: list[ItemCombinacion] = Field(min_length=1, max_length=20)
    inflacion_anual: Decimal | None = Field(
        default=None, description="Permite simular otro escenario. Por defecto, el INPC vigente."
    )


class SolicitudOptimizador(Esquema):
    monto_total: Decimal = Field(gt=0)
    horizonte_dias: int = Field(gt=0, le=3650)
    respetar_seguro: bool = Field(
        default=True,
        description="No asigna a una institución más de lo que cubre su fondo de protección",
    )
    excluir_rojas: bool = Field(
        default=True, description="Deja fuera a las instituciones con bandera roja activa"
    )
    inflacion_anual: Decimal | None = None


class AsignacionSchema(Esquema):
    institucion: InstitucionResumen
    producto_id: int
    producto: str
    producto_slug: str
    plazo_dias: int | None
    porcentaje: Decimal
    monto: Decimal
    ten: Decimal = Field(
        description="TEN efectiva del monto asignado: en un escalonado, la de la ponderada"
    )
    cascada: CascadaSchema
    escalonada: bool = False
    tramos: list[TramoSchema] = Field(default_factory=list)
    cobertura: CoberturaSchema
    monto_cubierto: Decimal
    monto_expuesto: Decimal
    cubierto: bool
    advertencia_liquidez: str | None = Field(
        description="El plazo del producto excede el horizonte elegido"
    )
    banderas: list[BanderaSchema]
    procedencia: Procedencia


class RespuestaCombinacion(Esquema):
    monto_total: Decimal = Field(description="Lo efectivamente repartido")
    monto_no_asignado: Decimal = Field(
        default=Decimal("0"),
        description=(
            "Sólo lo devuelve el optimizador: parte del monto que no pudo colocarse "
            "sin exceder algún límite de seguro. Estirarla entre los instrumentos ya "
            "llenos anularía la protección que se pidió respetar."
        ),
    )
    horizonte_dias: int
    ten_ponderada: Decimal

    rendimiento_bruto: Decimal
    isr_retenido: Decimal
    rendimiento_neto: Decimal
    efecto_inflacion: Decimal
    ganancia_real: Decimal

    monto_protegido: Decimal
    porcentaje_protegido: Decimal = Field(
        description="Truncado hacia abajo: nunca es 100 si queda un peso expuesto"
    )

    asignaciones: list[AsignacionSchema]
    narrativa: str
    nota_fiscal: str

    #: Contexto del cálculo, para que el resultado sea reproducible.
    inflacion_anual: Decimal
    valor_udi: Decimal
    generado_en: datetime
    aviso_optimizador: str = AVISO_OPTIMIZADOR
    disclaimer: str = DISCLAIMER


# ─── Meta ─────────────────────────────────────────────────────


class FrescuraFuente(Esquema):
    fuente: str
    ultima_actualizacion: date | None
    dias_desde_actualizacion: int | None
    #: `null` = fuente informativa, sin cadencia que vigilar (MANUAL, LLM).
    sla_dias: int | None
    dentro_de_sla: bool
    observaciones: int


class RespuestaFrescura(Esquema):
    """Obligación de §11: la fecha de cada dato, siempre visible."""

    fuentes: list[FrescuraFuente]
    ultima_actualizacion: date | None = Field(
        default=None,
        description="La más reciente de todas las fuentes. La UI la muestra en la cabecera.",
    )
    mostrar_tasas_sin_verificar: bool = Field(
        default=False,
        description=(
            "Si es true, el catálogo incluye las tasas en PENDIENTE_REVISION marcadas "
            "«sin verificar» — la política de transición del lanzamiento. Se publica "
            "aquí para que el estado sea observable desde fuera y no haya que leer "
            "los logs del servidor para saberlo."
        ),
    )
    generado_en: datetime
    todo_dentro_de_sla: bool


# ─── Admin ────────────────────────────────────────────────────


class AltaTasa(Esquema):
    """Alta manual por la API admin. No acepta tramos: la vía manual de las
    escaleras es el CSV (`cli tasas import`), que pasa por el validador."""

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
    "AVISO_OPTIMIZADOR",
    "DISCLAIMER",
    "AltaTasa",
    "AsignacionSchema",
    "BanderaSchema",
    "CascadaSchema",
    "CoberturaSchema",
    "DetalleInstitucion",
    "FilaComparador",
    "FrescuraFuente",
    "GatSchema",
    "IndicadorEvaluadoSchema",
    "IndicadoresSchema",
    "InstitucionResumen",
    "ItemCombinacion",
    "Procedencia",
    "ProductoDetalle",
    "ResolucionRevision",
    "RespuestaCalculadora",
    "RespuestaCombinacion",
    "RespuestaComparador",
    "RespuestaFrescura",
    "ResultadoCalculadora",
    "RevisionPendiente",
    "SolicitudCalculadora",
    "SolicitudCombinacion",
    "SolicitudOptimizador",
    "TasaCreada",
    "TramoSchema",
]
