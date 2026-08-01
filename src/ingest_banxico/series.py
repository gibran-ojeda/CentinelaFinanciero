"""Catálogo declarativo de las series del SIE que Centinela consume.

**Las claves no se asumen: están verificadas contra la API real.** Cada una de
las siete respondió 200 con el título que aparece en su comentario. El catálogo
completo del SIE, por si hay que añadir más:
https://www.banxico.org.mx/SieAPIRest/service/v1/doc/catalogoSeries

Dos usos distintos salen de aquí:

- `CATALOGO` alimenta `sync`, que guarda todas las series en `valores_serie`.
  De ahí leen la calculadora (INPC) y la conversión de coberturas (UDI).
- `CETES_POR_PLAZO` alimenta `materializer`, que convierte las cuatro series de
  subasta en filas de `tasas` sobre los productos CETES del catálogo.

Las demás series —TIIE, tipo de cambio, tasa objetivo— se sincronizan aunque
todavía nadie las lea: son el contexto macro de §11 y cuestan una fracción de
la misma petición que ya se hace.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Serie:
    """Una serie del SIE y cómo se guarda en `series_economicas`."""

    clave: str
    nombre: str
    unidad: str
    descripcion: str


#: Valor de la UDI. Convierte los límites de cobertura IPAB (400,000 UDIs) y
#: PROSOFIPO (25,000 UDIs) a pesos — ver `metrics.coverage`.
#:
#: **Banxico la publica con diez días de adelanto**, así que el «último dato»
#: de esta serie suele tener fecha futura. Quien la consume pide el último
#: valor con fecha ≤ hoy, no el máximo: ver `api.dependencies`.
UDI = Serie(
    clave="SP68257",
    nombre="Valor de la UDI",
    unidad="MXN por UDI",
    descripcion="Unidad de Inversión, publicada con diez días de anticipación",
)

#: Índice Nacional de Precios al Consumidor. La inflación anual sale de
#: comparar el índice de un mes contra el del mismo mes del año anterior, que
#: es lo que hace `api.dependencies._inflacion_anual` con trece observaciones.
INPC = Serie(
    clave="SP1",
    nombre="Índice Nacional de Precios al Consumidor",
    unidad="Índice base segunda quincena de julio 2018 = 100",
    descripcion="INPC general mensual; de aquí sale la inflación anual",
)

#: Series de subasta semanal de CETES, una por plazo.
CETES_28 = Serie(
    clave="SF43936",
    nombre="CETES 28 días",
    unidad="% anual",
    descripcion="Tasa de rendimiento de la subasta semanal, CETES a 28 días",
)
CETES_91 = Serie(
    clave="SF43939",
    nombre="CETES 91 días",
    unidad="% anual",
    descripcion="Tasa de rendimiento de la subasta semanal, CETES a 91 días",
)
CETES_182 = Serie(
    clave="SF43942",
    nombre="CETES 182 días",
    unidad="% anual",
    descripcion="Tasa de rendimiento de la subasta semanal, CETES a 182 días",
)
#: Ojo: **no se subasta todas las semanas.** En julio de 2026 sólo hubo
#: subasta el 9 y el 23. El materializador no rellena los huecos.
CETES_364 = Serie(
    clave="SF43945",
    nombre="CETES 364 días",
    unidad="% anual",
    descripcion="Tasa de rendimiento de la subasta semanal, CETES a 364 días",
)

TIIE_28 = Serie(
    clave="SF43783",
    nombre="TIIE a 28 días",
    unidad="% anual",
    descripcion="Tasa de Interés Interbancaria de Equilibrio a 28 días",
)
TIPO_DE_CAMBIO_FIX = Serie(
    clave="SF60653",
    nombre="Tipo de cambio FIX",
    unidad="MXN por USD",
    descripcion="Tipo de cambio para solventar obligaciones en moneda extranjera",
)
TASA_OBJETIVO = Serie(
    clave="SF61745",
    nombre="Tasa objetivo de Banxico",
    unidad="% anual",
    descripcion="Objetivo para la Tasa de Interés Interbancaria a un día",
)


CATALOGO: tuple[Serie, ...] = (
    UDI,
    INPC,
    CETES_28,
    CETES_91,
    CETES_182,
    CETES_364,
    TIIE_28,
    TIPO_DE_CAMBIO_FIX,
    TASA_OBJETIVO,
)

POR_CLAVE: dict[str, Serie] = {serie.clave: serie for serie in CATALOGO}

#: Serie de subasta → plazo del producto CETES que le corresponde en el
#: catálogo. Los cuatro productos ya existen en `seeds/productos.yaml` con
#: exactamente estos plazos.
CETES_POR_PLAZO: dict[str, int] = {
    CETES_28.clave: 28,
    CETES_91.clave: 91,
    CETES_182.clave: 182,
    CETES_364.clave: 364,
}

#: Cuántos días hacia atrás se piden la primera vez que se ve una serie. Tres
#: años cubren de sobra los trece meses de INPC que la inflación anual necesita
#: y dan historia suficiente para mirar una serie sin volver a pedirla.
DIAS_DE_CARGA_INICIAL = 365 * 3


def claves() -> list[str]:
    """Todas las claves del catálogo, en el orden en que se declararon."""
    return [serie.clave for serie in CATALOGO]


__all__ = [
    "CATALOGO",
    "CETES_28",
    "CETES_91",
    "CETES_182",
    "CETES_364",
    "CETES_POR_PLAZO",
    "DIAS_DE_CARGA_INICIAL",
    "INPC",
    "POR_CLAVE",
    "TASA_OBJETIVO",
    "TIIE_28",
    "TIPO_DE_CAMBIO_FIX",
    "UDI",
    "Serie",
    "claves",
]
