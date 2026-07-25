"""Modelos pydantic del dominio.

Separación explícita respecto al ORM (patrón NarrativeAlpha): las clases de
`orm.py` describen **cómo se persiste**, éstas describen **con qué trabaja la
lógica de negocio**. El puente son los `from_orm_*` de este módulo.

Por qué la separación importa aquí: `metrics/` debe ser puro y testeable sin
base de datos. Si recibiera objetos ORM, cada test necesitaría una sesión
viva y el motor de métricas dejaría de ser una función pura.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from domain import orm
from domain.enums import (
    CategoriaInstitucion,
    EstadoTasa,
    FuenteTasa,
    Liquidez,
    NivelCapitalizacion,
    Severidad,
    TipoBandera,
    TipoInstrumento,
    TipoProducto,
    TipoSeguro,
)


class DomainModel(BaseModel):
    """Base inmutable. Un modelo de dominio no se muta, se reemplaza."""

    model_config = ConfigDict(frozen=True, from_attributes=True)


class Institucion(DomainModel):
    id: int
    nombre: str
    slug: str
    categoria: CategoriaInstitucion
    tipo_seguro: TipoSeguro
    nombre_cnbv: str | None = None
    estatus_regulatorio: str | None = None
    url_sitio: str | None = None
    activa: bool = True
    es_demostracion: bool = False


class Producto(DomainModel):
    id: int
    institucion_id: int
    nombre: str
    slug: str
    tipo: TipoProducto
    instrumento: TipoInstrumento
    plazo_dias: int | None
    monto_minimo: Decimal
    liquidez: Liquidez
    penalizacion_retiro: str | None = None
    activo: bool = True

    @property
    def plazo_efectivo_dias(self) -> int:
        """Días que se usan para anualizar.

        Un producto a la vista no tiene plazo contractual, pero las métricas
        necesitan un horizonte: se toma el año completo, que es como se
        publican las tasas de vista.
        """
        return self.plazo_dias if self.plazo_dias is not None else 365


class Tasa(DomainModel):
    """Observación de tasa. Siempre viaja con `fecha_dato` y `fuente`.

    §11 (actualización transparente) y §19 (auditabilidad) obligan a que
    ninguna tasa se muestre sin su procedencia y su fecha. Por eso ambos
    campos son obligatorios en el modelo, no opcionales.
    """

    id: int
    producto_id: int
    tasa_nominal: Decimal
    gat_nominal: Decimal | None = None
    gat_real: Decimal | None = None
    fecha_dato: date
    fuente: FuenteTasa
    fuente_url: str | None = None
    estado: EstadoTasa = EstadoTasa.VIGENTE

    @property
    def publicable(self) -> bool:
        return self.estado is EstadoTasa.VIGENTE


class IndicadoresInstitucion(DomainModel):
    """Entrada del motor de banderas (§5.1).

    Todos los indicadores son opcionales porque la CNBV no publica todo para
    todas las figuras: una SOFIPO trae NICAP y un banco no. `flags.py` debe
    tolerar ausencias sin inventar banderas.
    """

    institucion_id: int
    periodo: date
    imor: Decimal | None = None
    icap: Decimal | None = None
    icor: Decimal | None = None
    nicap_nivel: NivelCapitalizacion | None = None
    captacion: Decimal | None = None
    cartera_total: Decimal | None = None
    capital_contable: Decimal | None = None
    pasivo_total: Decimal | None = None
    #: Crecimiento porcentual de captación respecto al periodo anterior. Lo
    #: calcula quien arma el objeto: es una derivada, no un dato de la CNBV.
    crecimiento_captacion_pct: Decimal | None = None

    @property
    def apalancamiento(self) -> Decimal | None:
        if self.pasivo_total is None or not self.capital_contable:
            return None
        return self.pasivo_total / self.capital_contable


class Bandera(DomainModel):
    """Bandera emitida por el motor. Sin `id`: aún no se ha persistido."""

    institucion_id: int
    tipo: TipoBandera
    severidad: Severidad
    motivo: str
    periodo_dato: date | None = None
    compuesta: bool = False
    """Las compuestas (§5.2) tienen prioridad y suprimen a las individuales."""


class UmbralesBanderas(DomainModel):
    """Umbrales inyectados en el motor de banderas.

    `flags.py` recibe este objeto y **no** importa ConfigStore: así el módulo
    sigue siendo puro (criterio de aceptación de la fase 3). Quien llama es
    responsable de construirlo desde `effective`.
    """

    imor_amarilla: Decimal = Decimal("3.0")
    imor_roja: Decimal = Decimal("6.0")
    icap_amarilla: Decimal = Decimal("15.0")
    icap_roja: Decimal = Decimal("10.5")
    cobertura_amarilla: Decimal = Decimal("100.0")
    cobertura_roja: Decimal = Decimal("70.0")
    gat_inconsistencia_pp: Decimal = Decimal("1.5")
    #: Crecimiento de captación a partir del cual se considera agresivo (§5.2).
    #: El nombre coincide con la clave `umbral_crecimiento_captacion_pct` del
    #: ConfigStore, sin el prefijo: construir este objeto desde `effective` es
    #: mapeo directo, sin tabla de traducción que se pueda desincronizar.
    crecimiento_captacion_pct: Decimal = Decimal("50.0")
    #: Cuántos puntos porcentuales sobre la mediana del mercado disparan la
    #: sospecha de "tasa de desesperación" (§5.2).
    tasa_sobre_mercado_pp: Decimal = Decimal("3.0")
    apalancamiento_amarilla: Decimal = Decimal("10.0")


class ParametrosFiscales(DomainModel):
    """Parámetros de retención vigentes. Los consume `metrics.fiscal`."""

    anio: int
    tasa_retencion_capital: Decimal
    vigente_desde: date
    fuente_url: str | None = None

    #: Retención sobre el interés generado, para instrumentos de base GANANCIA
    #: (§4.2). No se persiste todavía porque ningún instrumento del catálogo la
    #: usa: los fondos de deuda tributan sobre capital vía art. 87 → art. 54 de
    #: la LISR. Cuando la fase 10 incorpore una estructura que sí retenga sobre
    #: ganancia, esto pasa a ser columna de `parametros_fiscales`.
    tasa_retencion_ganancia: Decimal = Decimal("0")

    @property
    def nota_fiscal(self) -> str:
        """Texto que §6 obliga a mostrar junto a cualquier cálculo."""
        return (
            f"Retención de ISR del {self.tasa_retencion_capital}% anual sobre el capital, "
            f"vigente desde {self.vigente_desde.isoformat()} (ejercicio {self.anio})."
        )


class DesgloseCascada(DomainModel):
    """Los 5 conceptos de la calculadora (§6), en cascada y no en tabla."""

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


class ValorSerie(DomainModel):
    fecha: date
    valor: Decimal


class JobRunResumen(DomainModel):
    id: int
    job_id: str
    inicio: datetime
    fin: datetime | None = None
    estado: str
    metricas: dict[str, object] | None = Field(default=None)
    error: str | None = None


# ─── Puentes desde el ORM ─────────────────────────────────────


def from_orm_institucion(row: orm.Institucion) -> Institucion:
    return Institucion.model_validate(row)


def from_orm_producto(row: orm.Producto) -> Producto:
    return Producto.model_validate(row)


def from_orm_tasa(row: orm.Tasa) -> Tasa:
    return Tasa.model_validate(row)


def from_orm_indicadores(
    row: orm.IndicadorFinanciero,
    *,
    crecimiento_captacion_pct: Decimal | None = None,
) -> IndicadoresInstitucion:
    return IndicadoresInstitucion(
        institucion_id=row.institucion_id,
        periodo=row.periodo,
        imor=row.imor,
        icap=row.icap,
        icor=row.icor,
        nicap_nivel=row.nicap_nivel,
        captacion=row.captacion,
        cartera_total=row.cartera_total,
        capital_contable=row.capital_contable,
        pasivo_total=row.pasivo_total,
        crecimiento_captacion_pct=crecimiento_captacion_pct,
    )


def from_orm_parametros_fiscales(row: orm.ParametroFiscal) -> ParametrosFiscales:
    return ParametrosFiscales.model_validate(row)


__all__ = [
    "Bandera",
    "DesgloseCascada",
    "DomainModel",
    "IndicadoresInstitucion",
    "Institucion",
    "JobRunResumen",
    "ParametrosFiscales",
    "Producto",
    "Tasa",
    "UmbralesBanderas",
    "ValorSerie",
    "from_orm_indicadores",
    "from_orm_institucion",
    "from_orm_parametros_fiscales",
    "from_orm_producto",
    "from_orm_tasa",
]
