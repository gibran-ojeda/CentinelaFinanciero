"""Modelos SQLAlchemy 2.0 del esquema completo.

Convenciones del esquema:

- **Nombres de tabla y columna en español** cuando son conceptos del dominio
  regulatorio mexicano (§overview del plan). Los identificadores de código
  siguen en inglés salvo esos términos.
- **`Numeric` y nunca `Float`** para dinero, tasas e índices. Un `float` en un
  comparador financiero es un error de redondeo esperando ocurrir.
- **Enums como texto** con constraint de valores, no como tipos ENUM de
  Postgres: añadir un miembro no exige migrar un tipo.
- Las tablas de observaciones (`tasas`, `valores_serie`,
  `indicadores_financieros`) son **append-only**: se supersede con filas
  nuevas, nunca se borra ni se edita (§16 del foundation).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from domain.enums import (
    CategoriaInstitucion,
    EstadoJob,
    EstadoRevision,
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

#: JSONB en Postgres, JSON en cualquier otro motor (tests con SQLite).
JSONType = JSONB().with_variant(JSON(), "sqlite")

#: Porcentajes y tasas: hasta 999.9999 con cuatro decimales. Suficiente para
#: un IMOR de 12.3456% o un ICAP de 187.5%.
Porcentaje = Numeric(8, 4)

#: Importes en MXN: hasta 10^12 con dos decimales.
Dinero = Numeric(18, 2)

#: Valores de series económicas (UDI, INPC, TIIE): seis decimales.
ValorSerie = Numeric(18, 6)


def _enum_check(column: str, enum: type[StrEnum]) -> CheckConstraint:
    """Constraint que restringe una columna de texto a los valores del enum."""
    valores = ", ".join(f"'{m.value}'" for m in enum)
    return CheckConstraint(f"{column} IN ({valores})", name=f"ck_{column}_valido")


class Base(DeclarativeBase):
    """Base declarativa. `Base.metadata` es el objetivo de Alembic."""

    type_annotation_map = {
        Decimal: Numeric(18, 6),
        dict[str, Any]: JSONType,
    }


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ─── Catálogo ─────────────────────────────────────────────────


class Institucion(TimestampMixin, Base):
    __tablename__ = "instituciones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    #: Nombre exacto con el que aparece en los boletines de la CNBV. Es la
    #: clave de mapeo de la fase 8: la CNBV escribe "BANCO NU MEXICO, S.A." y
    #: nosotros mostramos "Nu México".
    nombre_cnbv: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    categoria: Mapped[CategoriaInstitucion] = mapped_column(String(24), nullable=False)
    tipo_seguro: Mapped[TipoSeguro] = mapped_column(String(16), nullable=False)
    estatus_regulatorio: Mapped[str | None] = mapped_column(String(200), nullable=True)
    url_sitio: Mapped[str | None] = mapped_column(Text, nullable=True)
    activa: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notas: Mapped[str | None] = mapped_column(Text, nullable=True)

    productos: Mapped[list[Producto]] = relationship(
        back_populates="institucion", cascade="all, delete-orphan"
    )
    indicadores: Mapped[list[IndicadorFinanciero]] = relationship(
        back_populates="institucion", cascade="all, delete-orphan"
    )
    banderas: Mapped[list[Bandera]] = relationship(
        back_populates="institucion", cascade="all, delete-orphan"
    )

    __table_args__ = (
        _enum_check("categoria", CategoriaInstitucion),
        _enum_check("tipo_seguro", TipoSeguro),
        Index("ix_instituciones_categoria_activa", "categoria", "activa"),
    )


class Producto(TimestampMixin, Base):
    __tablename__ = "productos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    institucion_id: Mapped[int] = mapped_column(
        ForeignKey("instituciones.id", ondelete="CASCADE"), nullable=False
    )
    nombre: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(180), nullable=False, unique=True)
    tipo: Mapped[TipoProducto] = mapped_column(String(8), nullable=False)
    #: Determina el tratamiento fiscal. No se deriva de la categoría de la
    #: institución: un banco puede ofrecer PRLV y también fondos de deuda.
    instrumento: Mapped[TipoInstrumento] = mapped_column(String(24), nullable=False)
    plazo_dias: Mapped[int | None] = mapped_column(Integer, nullable=True)
    monto_minimo: Mapped[Decimal] = mapped_column(Dinero, nullable=False, default=Decimal("0"))
    liquidez: Mapped[Liquidez] = mapped_column(String(20), nullable=False)
    penalizacion_retiro: Mapped[str | None] = mapped_column(Text, nullable=True)
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    institucion: Mapped[Institucion] = relationship(back_populates="productos")
    tasas: Mapped[list[Tasa]] = relationship(
        back_populates="producto", cascade="all, delete-orphan"
    )

    __table_args__ = (
        _enum_check("tipo", TipoProducto),
        _enum_check("instrumento", TipoInstrumento),
        _enum_check("liquidez", Liquidez),
        # Clave natural para el upsert idempotente del `cli seed`.
        UniqueConstraint("institucion_id", "nombre", "plazo_dias", name="uq_producto_natural"),
        # VISTA no lleva plazo; PLAZO lo exige. Sin esto se cuelan productos
        # que el comparador no sabe clasificar en el filtro de §7.
        CheckConstraint(
            "(tipo = 'VISTA' AND plazo_dias IS NULL) "
            "OR (tipo = 'PLAZO' AND plazo_dias IS NOT NULL AND plazo_dias > 0)",
            name="ck_plazo_coherente_con_tipo",
        ),
        CheckConstraint("monto_minimo >= 0", name="ck_monto_minimo_no_negativo"),
        Index("ix_productos_institucion_activo", "institucion_id", "activo"),
        Index("ix_productos_plazo", "tipo", "plazo_dias"),
    )


# ─── Observaciones ────────────────────────────────────────────


class Tasa(TimestampMixin, Base):
    """Observación de tasa. **Append-only**: una fila por lectura.

    La vigente de un producto es la fila con `fecha_dato` más reciente en
    estado VIGENTE. Nunca se actualiza ni se borra una tasa: se supersede.
    """

    __tablename__ = "tasas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    producto_id: Mapped[int] = mapped_column(
        ForeignKey("productos.id", ondelete="CASCADE"), nullable=False
    )
    tasa_nominal: Mapped[Decimal] = mapped_column(Porcentaje, nullable=False)
    gat_nominal: Mapped[Decimal | None] = mapped_column(Porcentaje, nullable=True)
    gat_real: Mapped[Decimal | None] = mapped_column(Porcentaje, nullable=True)
    fecha_dato: Mapped[date] = mapped_column(Date, nullable=False)
    fuente: Mapped[FuenteTasa] = mapped_column(String(20), nullable=False)
    fuente_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    estado: Mapped[EstadoTasa] = mapped_column(
        String(20), nullable=False, default=EstadoTasa.VIGENTE
    )
    notas: Mapped[str | None] = mapped_column(Text, nullable=True)

    producto: Mapped[Producto] = relationship(back_populates="tasas")

    __table_args__ = (
        _enum_check("fuente", FuenteTasa),
        _enum_check("estado", EstadoTasa),
        CheckConstraint("tasa_nominal >= 0", name="ck_tasa_nominal_no_negativa"),
        # Idempotencia de la carga: reimportar el mismo CSV no duplica.
        UniqueConstraint("producto_id", "fecha_dato", "fuente", name="uq_tasa_observacion"),
        Index("ix_tasas_vigentes", "producto_id", "estado", "fecha_dato"),
    )


class IndicadorFinanciero(TimestampMixin, Base):
    """Indicadores de salud institucional por periodo (§5.1). Fuente: CNBV."""

    __tablename__ = "indicadores_financieros"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    institucion_id: Mapped[int] = mapped_column(
        ForeignKey("instituciones.id", ondelete="CASCADE"), nullable=False
    )
    periodo: Mapped[date] = mapped_column(Date, nullable=False)
    imor: Mapped[Decimal | None] = mapped_column(Porcentaje, nullable=True)
    icap: Mapped[Decimal | None] = mapped_column(Porcentaje, nullable=True)
    icor: Mapped[Decimal | None] = mapped_column(Porcentaje, nullable=True)
    """Índice de cobertura de cartera vencida (reservas / cartera vencida)."""

    nicap_nivel: Mapped[NivelCapitalizacion | None] = mapped_column(String(4), nullable=True)
    captacion: Mapped[Decimal | None] = mapped_column(Dinero, nullable=True)
    cartera_total: Mapped[Decimal | None] = mapped_column(Dinero, nullable=True)
    capital_contable: Mapped[Decimal | None] = mapped_column(Dinero, nullable=True)
    pasivo_total: Mapped[Decimal | None] = mapped_column(Dinero, nullable=True)
    fuente_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    institucion: Mapped[Institucion] = relationship(back_populates="indicadores")

    __table_args__ = (
        _enum_check("nicap_nivel", NivelCapitalizacion),
        UniqueConstraint("institucion_id", "periodo", name="uq_indicador_periodo"),
        Index("ix_indicadores_periodo", "periodo"),
    )


class Bandera(TimestampMixin, Base):
    """Bandera de riesgo institucional. La sincroniza `banderas_recompute`."""

    __tablename__ = "banderas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    institucion_id: Mapped[int] = mapped_column(
        ForeignKey("instituciones.id", ondelete="CASCADE"), nullable=False
    )
    tipo: Mapped[TipoBandera] = mapped_column(String(28), nullable=False)
    severidad: Mapped[Severidad] = mapped_column(String(10), nullable=False)
    motivo: Mapped[str] = mapped_column(Text, nullable=False)
    #: Periodo del dato que la originó. §11 obliga a mostrarlo siempre: una
    #: bandera de la CNBV puede venir con tres meses de rezago.
    periodo_dato: Mapped[date | None] = mapped_column(Date, nullable=True)
    activa: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    resuelta_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    institucion: Mapped[Institucion] = relationship(back_populates="banderas")

    __table_args__ = (
        _enum_check("tipo", TipoBandera),
        _enum_check("severidad", Severidad),
        Index("ix_banderas_institucion_activa", "institucion_id", "activa"),
    )


# ─── Series económicas ────────────────────────────────────────


class SerieEconomica(TimestampMixin, Base):
    """Catálogo de series de Banxico (UDI, INPC, TIIE, subastas)."""

    __tablename__ = "series_economicas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    clave_banxico: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    nombre: Mapped[str] = mapped_column(String(160), nullable=False)
    unidad: Mapped[str] = mapped_column(String(40), nullable=False)
    descripcion: Mapped[str | None] = mapped_column(Text, nullable=True)
    activa: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    valores: Mapped[list[ValorSerieEconomica]] = relationship(
        back_populates="serie", cascade="all, delete-orphan"
    )


class ValorSerieEconomica(Base):
    __tablename__ = "valores_serie"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    serie_id: Mapped[int] = mapped_column(
        ForeignKey("series_economicas.id", ondelete="CASCADE"), nullable=False
    )
    fecha: Mapped[date] = mapped_column(Date, nullable=False)
    valor: Mapped[Decimal] = mapped_column(ValorSerie, nullable=False)

    serie: Mapped[SerieEconomica] = relationship(back_populates="valores")

    __table_args__ = (
        UniqueConstraint("serie_id", "fecha", name="uq_valor_serie_fecha"),
        Index("ix_valores_serie_fecha", "serie_id", "fecha"),
    )


# ─── Parámetros y fuentes ─────────────────────────────────────


class ParametroFiscal(TimestampMixin, Base):
    """Retención de ISR por ejercicio fiscal (cambia por Ley de Ingresos).

    El *tratamiento* por instrumento (sobre capital vs. sobre ganancia) vive en
    `metrics.fiscal`; aquí sólo la tasa, que es lo que cambia cada año.
    """

    __tablename__ = "parametros_fiscales"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    anio: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    tasa_retencion_capital: Mapped[Decimal] = mapped_column(Porcentaje, nullable=False)
    vigente_desde: Mapped[date] = mapped_column(Date, nullable=False)
    fuente_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    notas: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "tasa_retencion_capital >= 0 AND tasa_retencion_capital <= 100",
            name="ck_retencion_en_rango",
        ),
    )


class FuenteTasas(TimestampMixin, Base):
    """URL curada por institución para el fetch dirigido de la fase 9."""

    __tablename__ = "fuentes_tasas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    institucion_id: Mapped[int] = mapped_column(
        ForeignKey("instituciones.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    nivel: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    requiere_js: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    activa: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    ultima_extraccion_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Hash del contenido de la última descarga: si no cambió, no se paga LLM.
    ultimo_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint("nivel IN (2, 3)", name="ck_nivel_de_fuente"),
        UniqueConstraint("institucion_id", "url", name="uq_fuente_url"),
    )


class RevisionTasa(TimestampMixin, Base):
    """Cola de revisión humana de extracciones LLM (§15). Se llena en fase 9."""

    __tablename__ = "revisiones_tasas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tasa_id: Mapped[int] = mapped_column(
        ForeignKey("tasas.id", ondelete="CASCADE"), nullable=False
    )
    motivo: Mapped[str] = mapped_column(Text, nullable=False)
    valor_anterior: Mapped[Decimal | None] = mapped_column(Porcentaje, nullable=True)
    valor_nuevo: Mapped[Decimal] = mapped_column(Porcentaje, nullable=False)
    estado: Mapped[EstadoRevision] = mapped_column(
        String(12), nullable=False, default=EstadoRevision.PENDIENTE
    )
    revisor: Mapped[str | None] = mapped_column(String(80), nullable=True)
    resuelto_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        _enum_check("estado", EstadoRevision),
        Index("ix_revisiones_estado", "estado"),
    )


# ─── Configuración y operación ────────────────────────────────


class ConfigStoreEntry(Base):
    """Valor activo de un parámetro de negocio ajustable sin deploy."""

    __tablename__ = "config_store"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    value_type: Mapped[str] = mapped_column(String(16), nullable=False)
    grupo: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    updated_by: Mapped[str | None] = mapped_column(String(80), nullable=True)

    __table_args__ = (Index("ix_config_store_grupo", "grupo"),)


class ConfigVersion(TimestampMixin, Base):
    """Historial de cambios de configuración.

    §19 exige poder reconstruir qué umbral estaba vigente cuándo, y por tanto
    qué bandera generó. Sin este historial, una bandera pasada es inauditable.
    """

    __tablename__ = "config_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    value_type: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    motivo: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(80), nullable=True)

    __table_args__ = (
        UniqueConstraint("key", "version", name="uq_config_version"),
        Index("ix_config_versions_key", "key"),
    )


class JobRun(Base):
    """Bitácora de cada corrida de job (§13, observabilidad)."""

    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(String(60), nullable=False)
    inicio: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    fin: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    estado: Mapped[EstadoJob] = mapped_column(
        String(12), nullable=False, default=EstadoJob.EN_CURSO
    )
    metricas: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        _enum_check("estado", EstadoJob),
        Index("ix_job_runs_job_inicio", "job_id", "inicio"),
    )


__all__ = [
    "Bandera",
    "Base",
    "ConfigStoreEntry",
    "ConfigVersion",
    "FuenteTasas",
    "IndicadorFinanciero",
    "Institucion",
    "JobRun",
    "ParametroFiscal",
    "Producto",
    "RevisionTasa",
    "SerieEconomica",
    "Tasa",
    "ValorSerieEconomica",
]
