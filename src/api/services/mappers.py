"""Conversión de objetos de dominio a esquemas de respuesta.

Existe para que los tres routers que muestran tasas —comparador, detalle de
institución y calculadora— produzcan exactamente la misma forma para los mismos
conceptos. Si cada uno armara su GAT o su cobertura por su cuenta, acabarían
divergiendo y el frontend tendría que tratarlos como tres contratos distintos.
"""

from __future__ import annotations

from decimal import Decimal

from api.schemas import (
    BanderaSchema,
    CoberturaSchema,
    GatSchema,
    InstitucionResumen,
    Procedencia,
    TramoSchema,
)
from domain import orm
from domain.enums import EstadoTasa
from domain.models import Bandera
from metrics.coverage import Cobertura, resolver_cobertura
from metrics.gat import Gat
from metrics.tramos import Tramo


def institucion_resumen(institucion: orm.Institucion) -> InstitucionResumen:
    return InstitucionResumen(
        id=institucion.id,
        nombre=institucion.nombre,
        slug=institucion.slug,
        categoria=institucion.categoria,
        tipo_seguro=institucion.tipo_seguro,
        es_demostracion=institucion.es_demostracion,
    )


def procedencia(tasa: orm.Tasa) -> Procedencia:
    return Procedencia(
        fecha_dato=tasa.fecha_dato,
        fuente=tasa.fuente,
        fuente_url=tasa.fuente_url,
        estado=tasa.estado,
        # Una sola definición de "verificada", aquí y no en cada router: es
        # exactamente lo que significa el estado VIGENTE, y la UI no debería
        # tener que conocer la máquina de estados para saber si marcar la fila.
        verificada=tasa.estado is EstadoTasa.VIGENTE,
    )


def tramos_schema(tasa: orm.Tasa) -> list[TramoSchema]:
    """La escalera de una observación para la respuesta; vacía si es plana.

    Llega ya ordenada por piso: el `order_by` de la relación lo garantiza.
    """
    return [
        TramoSchema(desde=t.desde, hasta=t.hasta, tasa_nominal=t.tasa_nominal) for t in tasa.tramos
    ]


def tramos_de(tasa: orm.Tasa) -> tuple[Tramo, ...]:
    """La escalera de una observación como valores de `metrics`; `()` si es plana.

    Es el único puente ORM→métricas de tramos: `metrics/` no puede importar el
    ORM y cada consumidor repitiendo la conversión acabaría divergiendo.
    """
    return tuple(
        Tramo(desde=t.desde, hasta=t.hasta, tasa_nominal=t.tasa_nominal) for t in tasa.tramos
    )


def gat_schema(gat: Gat) -> GatSchema:
    return GatSchema(
        nominal=gat.nominal,
        real=gat.real,
        origen=gat.origen.value,
        es_calculada=gat.es_calculada,
    )


def cobertura_schema(cobertura: Cobertura) -> CoberturaSchema:
    return CoberturaSchema(
        tipo=cobertura.tipo,
        limite_udis=cobertura.limite_udis,
        limite_mxn=cobertura.limite_mxn,
        valor_udi=cobertura.valor_udi,
        sin_limite=cobertura.sin_limite,
        sin_cobertura=cobertura.sin_cobertura,
    )


def cobertura_de(institucion: orm.Institucion, valor_udi: Decimal) -> CoberturaSchema:
    return cobertura_schema(resolver_cobertura(institucion.tipo_seguro, valor_udi))


def bandera_schema(bandera: Bandera) -> BanderaSchema:
    return BanderaSchema(
        tipo=bandera.tipo,
        severidad=bandera.severidad,
        motivo=bandera.motivo,
        periodo_dato=bandera.periodo_dato,
        compuesta=bandera.compuesta,
    )


def bandera_desde_orm(fila: orm.Bandera) -> BanderaSchema:
    return BanderaSchema(
        tipo=fila.tipo,
        severidad=fila.severidad,
        motivo=fila.motivo,
        periodo_dato=fila.periodo_dato,
        # La tabla no guarda si era compuesta; se deriva del tipo, que es la
        # fuente de verdad de esa distinción.
        compuesta=fila.tipo.value in {"NO_RECOMENDABLE", "RED_FLAG_TASA", "GAT_INCONSISTENTE"},
    )


__all__ = [
    "bandera_desde_orm",
    "bandera_schema",
    "cobertura_de",
    "cobertura_schema",
    "gat_schema",
    "institucion_resumen",
    "procedencia",
    "tramos_de",
    "tramos_schema",
]
