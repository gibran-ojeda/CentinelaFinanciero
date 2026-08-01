"""Qué publicaciones de la CNBV se leen y dónde vive cada indicador.

**Nada de esto se dedujo del portal: se midió** descargando los archivos reales
en julio de 2026 (`BE BM 202605.xlsx` y `BE_SOFIPOS_202603.xlsx`).

Dónde está cada indicador, que es lo que costó encontrar:

| Indicador  | Banca múltiple            | SOFIPOs                          |
|------------|---------------------------|----------------------------------|
| IMOR       | hoja `CCT`                | hoja `Sociedades_2`              |
| ICOR       | hoja `CCT`                | — (sólo agregado del sector)     |
| ICAP       | hoja `Art_121`            | —                                |
| NICAP      | — (bancos usan categoría) | **sólo en el PDF de NCyAT**      |
| Captación  | hoja `CaptRec`            | hoja `Tasas_Implícitas`          |
| Cartera    | hoja `CCT`                | hoja `Sociedades_2`              |

## Por qué las columnas no se fijan por número

Cada concepto ocupa **tres columnas**: el mismo mes del año anterior, el
periodo anterior y el vigente. En `CCT`, «IMOR (%)» abre en la columna F pero
el dato de mayo de 2026 está en la H — la F es mayo de **2025**. Fijar «la
tercera» funcionaría hoy y fallaría en silencio el día que la CNBV muestre dos
periodos o cuatro, cargando cifras de otro año como si fueran las de éste.

Así que se declara **dónde empieza el bloque de cada concepto** y el parser
busca, dentro del bloque, la columna cuyo periodo coincide con el del boletín.
Si ninguna coincide, se rompe con un error que lo dice — que es justo lo que
§8 exige: «un cambio de formato rompe el job con error explícito, nunca carga
datos malinterpretados».
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from domain.enums import CategoriaInstitucion

SECTOR_BANCA = "Banca Múltiple"
SECTOR_SOFIPO = "Sociedades Financieras Populares"

TEMA_BOLETINES = "Boletines"
TEMA_NCYAT = "Nivel de Capitalización y Alertas Tempranas"


@dataclass(frozen=True, slots=True)
class Fuente:
    """Una publicación que la ingesta lee, con su formato esperado."""

    clave: str
    sector: str
    tema: str
    extension: str
    descripcion: str
    #: Columna de `indicadores_financieros` que **sólo** llena esta fuente. Es
    #: cómo se sabe si un periodo ya se cargó sin necesidad de una tabla de
    #: control: si esa columna ya tiene valor para ese periodo, esta fuente ya
    #: corrió. Preguntar sólo por el periodo no bastaría — el boletín de
    #: SOFIPOs y el PDF de NICAP pueden coincidir en mes, y el segundo se
    #: saltaría por el trabajo del primero.
    columna_testigo: str
    #: Figuras del catálogo que esta publicación cubre. **Acota el reporte de
    #: mapeo, nunca el casamiento**: la CNBV publica a Nu México entre las
    #: SOFIPOs aunque el catálogo lo tenga como banco digital, y filtrar
    #: candidatas por figura lo dejaría sin indicadores. Lo que sí corrige es
    #: el ruido: sin esto, «sin_nombre_cnbv» nombraba al Gobierno Federal en
    #: cada corrida y el boletín de banca listaba a cada SOFIPO como «sin
    #: datos» — y una señal con ruido permanente se aprende a ignorar.
    categorias: frozenset[CategoriaInstitucion]


#: Boletín estadístico de banca múltiple. **Mensual.** Trae IMOR, ICOR, ICAP y
#: captación de cada banco: todo lo que las banderas necesitan para las cinco
#: instituciones de banca digital del catálogo.
BOLETIN_BANCA = Fuente(
    clave="boletin_banca",
    sector=SECTOR_BANCA,
    tema=TEMA_BOLETINES,
    extension="xlsx",
    descripcion="Boletín estadístico de banca múltiple (mensual)",
    columna_testigo="imor",
    categorias=frozenset(
        {CategoriaInstitucion.BANCO_DIGITAL, CategoriaInstitucion.BANCO_TRADICIONAL}
    ),
)

#: Boletín estadístico de SOFIPOs. **Trimestral**, y por eso va siempre más
#: atrasado que el de banca: en julio de 2026 el último era de marzo.
BOLETIN_SOFIPO = Fuente(
    clave="boletin_sofipo",
    sector=SECTOR_SOFIPO,
    tema=TEMA_BOLETINES,
    extension="xlsx",
    descripcion="Boletín estadístico de SOFIPOs (trimestral)",
    columna_testigo="imor",
    categorias=frozenset({CategoriaInstitucion.SOFIPO}),
)

#: Nivel de capitalización de SOFIPOs. Mensual y **sólo en PDF**: es la única
#: fuente del NICAP, la categoría prudencial N1–N4 de §5.1, que no se deduce
#: del ICAP ni de ningún otro número del boletín.
NCYAT_SOFIPO = Fuente(
    clave="ncyat_sofipo",
    sector=SECTOR_SOFIPO,
    tema=TEMA_NCYAT,
    extension="pdf",
    descripcion="Nivel de capitalización de SOFIPOs (mensual, PDF)",
    columna_testigo="nicap_nivel",
    categorias=frozenset({CategoriaInstitucion.SOFIPO}),
)

FUENTES: tuple[Fuente, ...] = (BOLETIN_BANCA, BOLETIN_SOFIPO, NCYAT_SOFIPO)

POR_CLAVE: dict[str, Fuente] = {f.clave: f for f in FUENTES}


# ─── Dónde está cada cosa dentro de cada libro ────────────────


@dataclass(frozen=True, slots=True)
class Concepto:
    """Un indicador dentro de una hoja.

    `columna` es donde **empieza** su bloque de periodos, no donde está el dato
    vigente. `encabezado` es el texto con el que la CNBV lo titula; el parser
    lo verifica antes de leer una sola cifra.
    """

    campo: str
    columna: int
    encabezado: str
    #: Cuántas columnas ocupa el bloque. El parser busca dentro de ese rango la
    #: que corresponda al periodo del boletín.
    ancho: int = 3
    #: A qué multiplicar para llegar a pesos. **Las dos publicaciones usan
    #: unidades distintas**: banca múltiple viene en millones («Millones de
    #: pesos y porcentajes») y SOFIPOs en miles («miles de pesos»). Cargar sin
    #: convertir dejaría la captación de un banco mil veces por debajo de la de
    #: una SOFIPO, y el comparador las pone una al lado de la otra.
    factor: Decimal = Decimal(1)


MILLONES = Decimal(1_000_000)
MILES = Decimal(1_000)


@dataclass(frozen=True, slots=True)
class Hoja:
    """Una hoja del libro y cómo leerla.

    `fila_periodo` es la que dice a qué mes corresponde cada columna. Cuando es
    `None`, la hoja publica un solo periodo —el del propio boletín— y cada
    concepto ocupa una columna.
    """

    nombre: str
    fila_encabezado: int
    fila_datos: int
    col_institucion: int
    conceptos: tuple[Concepto, ...]
    fila_periodo: int | None = None
    #: Filas cuyo «nombre de institución» es en realidad un agregado. No son
    #: instituciones y cargarlas metería el sistema entero como si fuera un
    #: banco más.
    agregados: frozenset[str] = field(
        default_factory=lambda: frozenset({"sistema", "total del sector", "total"})
    )


#: Banca múltiple, «cartera de crédito total»: una fila por banco con su
#: cartera, su morosidad y su cobertura de cartera vencida.
BANCA_CARTERA = Hoja(
    nombre="CCT",
    fila_encabezado=5,
    fila_periodo=6,
    fila_datos=7,
    col_institucion=2,
    conceptos=(
        Concepto("cartera_total", 3, "Cartera total", factor=MILLONES),
        # IMOR e ICOR son porcentajes: no se convierten.
        Concepto("imor", 6, "IMOR"),
        Concepto("icor", 9, "ICOR"),
    ),
)

#: `Art_121` publica los indicadores de capitalización que exige ese artículo.
#: Un solo periodo, una columna por concepto — de ahí `fila_periodo=None`.
BANCA_CAPITAL = Hoja(
    nombre="Art_121",
    fila_encabezado=8,
    fila_datos=9,
    col_institucion=2,
    conceptos=(
        Concepto("icap", 5, "ICAP", ancho=1),
        Concepto("categoria", 6, "Categoría", ancho=1),
    ),
)

#: Saldos de captación bancaria.
BANCA_CAPTACION = Hoja(
    nombre="CaptRec",
    fila_encabezado=5,
    fila_periodo=6,
    fila_datos=7,
    col_institucion=2,
    conceptos=(Concepto("captacion", 3, "Captación total", factor=MILLONES),),
)

#: SOFIPOs: cartera por etapa e índice de morosidad.
SOFIPO_CARTERA = Hoja(
    nombre="Sociedades_2",
    fila_encabezado=12,
    fila_periodo=13,
    fila_datos=14,
    col_institucion=5,
    conceptos=(
        Concepto("cartera_vigente", 6, "Cartera de crédito etapa 1", factor=MILES),
        Concepto("cartera_etapa3", 9, "Cartera de crédito etapa 3", factor=MILES),
        Concepto("imor", 12, "Índice de morosidad"),
    ),
)

#: SOFIPOs: clientes, sucursales y activo total.
SOFIPO_ACTIVO = Hoja(
    nombre="Sociedades_1",
    fila_encabezado=12,
    fila_periodo=13,
    fila_datos=14,
    col_institucion=5,
    conceptos=(Concepto("activo_total", 12, "Activo total", factor=MILES),),
)

#: SOFIPOs: captación tradicional y su tasa implícita.
SOFIPO_CAPTACION = Hoja(
    nombre="Tasas_Implícitas",
    fila_encabezado=12,
    fila_periodo=13,
    fila_datos=14,
    col_institucion=5,
    conceptos=(Concepto("captacion", 10, "Captación tradicional", factor=MILES),),
)

HOJAS_BANCA: tuple[Hoja, ...] = (BANCA_CARTERA, BANCA_CAPITAL, BANCA_CAPTACION)
HOJAS_SOFIPO: tuple[Hoja, ...] = (SOFIPO_CARTERA, SOFIPO_ACTIVO, SOFIPO_CAPTACION)


__all__ = [
    "BANCA_CAPITAL",
    "BANCA_CAPTACION",
    "BANCA_CARTERA",
    "BOLETIN_BANCA",
    "BOLETIN_SOFIPO",
    "FUENTES",
    "HOJAS_BANCA",
    "HOJAS_SOFIPO",
    "NCYAT_SOFIPO",
    "POR_CLAVE",
    "SECTOR_BANCA",
    "SECTOR_SOFIPO",
    "SOFIPO_ACTIVO",
    "SOFIPO_CAPTACION",
    "SOFIPO_CARTERA",
    "TEMA_BOLETINES",
    "TEMA_NCYAT",
    "Concepto",
    "Fuente",
    "Hoja",
]
