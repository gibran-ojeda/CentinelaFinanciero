"""Vocabulario del dominio financiero mexicano.

Los valores son español porque son términos regulatorios sin traducción
natural (SOFIPO, PROSOFIPO, GAT, PRLV). Los nombres de los miembros y de las
clases son código, y el código va en inglés salvo justo estos términos.

Referencias al foundation: §3 (instrumentos), §4.6 (cobertura de seguro),
§15 (estrategia de datos) y §16 (modelo de datos).
"""

from __future__ import annotations

from enum import StrEnum


class CategoriaInstitucion(StrEnum):
    """Clasificación por **figura regulatoria vigente**, no por percepción.

    El principio de §3: Nu México dejó de ser SOFIPO en abril de 2025 y pasó a
    BANCO_DIGITAL; Mercado Pago sigue siendo IFPE aunque opere como banco a
    ojos del usuario. La categoría determina la cobertura de seguro.
    """

    GOBIERNO = "GOBIERNO"
    SOFIPO = "SOFIPO"
    BANCO_DIGITAL = "BANCO_DIGITAL"
    BANCO_TRADICIONAL = "BANCO_TRADICIONAL"
    IFPE = "IFPE"


class TipoSeguro(StrEnum):
    """Fondo que protege los depósitos (§4.6).

    Los límites viven en UDIs, no en pesos: ver `metrics.coverage`.
    """

    SOBERANO = "SOBERANO"
    """Deuda del Gobierno Federal. Sin límite de cobertura."""

    IPAB = "IPAB"
    """Banca múltiple. 400,000 UDIs."""

    PROSOFIPO = "PROSOFIPO"
    """Sociedades financieras populares. 25,000 UDIs."""

    NINGUNO = "NINGUNO"
    """IFPEs y similares: fondos en fideicomiso, sin fondo de protección."""


class TipoProducto(StrEnum):
    VISTA = "VISTA"
    """Disponibilidad inmediata. `plazo_dias` es nulo."""

    PLAZO = "PLAZO"
    """Plazo fijo. `plazo_dias` es obligatorio."""


class TipoInstrumento(StrEnum):
    """Determina el tratamiento fiscal (§4.2), no la institución que lo emite.

    Es lo que consume `metrics.fiscal` para decidir si la retención de ISR va
    sobre capital o sobre ganancia.
    """

    CETES = "CETES"
    BONDDIA = "BONDDIA"
    BONOS_M = "BONOS_M"
    BONDES_D = "BONDES_D"
    UDIBONOS = "UDIBONOS"
    PRLV = "PRLV"
    DEPOSITO_SOFIPO = "DEPOSITO_SOFIPO"
    DEPOSITO_BANCARIO = "DEPOSITO_BANCARIO"
    FONDO_DEUDA = "FONDO_DEUDA"
    MONEDERO_ELECTRONICO = "MONEDERO_ELECTRONICO"


class FuenteTasa(StrEnum):
    """Procedencia del dato. Determina el SLA de frescura y si pasa revisión."""

    MANUAL = "MANUAL"
    """Carga por CLI leyendo la publicación de la propia institución."""

    AGREGADOR = "AGREGADOR"
    """Dato recopilado por un tercero (otro comparador, la prensa).

    **Nunca puede estar VIGENTE**, y por tanto nunca llega al sitio público:
    un comparador que republica lo que recopiló otro no tiene fuente propia y
    no puede responder de un número que nadie de aquí leyó en su origen. La
    invariante se hace cumplir al escribir —en el alta por CSV y en el
    reviewer— y no filtrando al leer, para que no dependa de que cada consulta
    se acuerde de excluirla.

    Mientras `mostrar_tasas_sin_verificar` está activa —la política de
    transición del lanzamiento— sí se muestra, marcada «sin verificar» como
    cualquier otra `PENDIENTE_REVISION`, hasta que la lectura oficial de su
    producto la sustituye: la ventana de vigencia prefiere VIGENTE por
    estado, así que la sustitución es automática producto a producto.

    Existe por una razón operativa: sirve de **contraste**. Cuando llega la
    lectura oficial, el valor del agregador es el `valor_anterior` contra el
    que el reviewer mide la diferencia — que coincidan respalda la lectura, y
    una discrepancia grande la manda a revisión humana. En cuanto la lectura
    oficial la sustituye, la fila se retira del catálogo semilla.
    """

    BANXICO_API = "BANXICO_API"
    """SIE de Banxico. Nivel 1: determinista (fase 7)."""

    CNBV = "CNBV"
    """Portafolio de Información de la CNBV. Nivel 1 (fase 8)."""

    FETCH_DIRIGIDO = "FETCH_DIRIGIDO"
    """Descarga determinista + extracción LLM sobre URL curada (fase 9)."""

    LLM_RESEARCH = "LLM_RESEARCH"
    """Búsqueda abierta con agente. Sólo descubrimiento y verificación."""


class EstadoTasa(StrEnum):
    """Estado de una observación de tasa.

    La tabla `tasas` es append-only: una tasa nunca se edita ni se borra, se
    supersede con una fila nueva. La vigente es la más reciente en VIGENTE.
    """

    VIGENTE = "VIGENTE"
    PENDIENTE_REVISION = "PENDIENTE_REVISION"
    """Fuera de tolerancia o sin verificar. **No se afirma.**

    Mientras `mostrar_tasas_sin_verificar` está activa se publica marcada con
    `procedencia.verificada = false` — se amplía lo que se enseña, nunca lo
    que se afirma— y una pendiente jamás desplaza a una VIGENTE del mismo
    producto.
    """

    RECHAZADA = "RECHAZADA"


class Severidad(StrEnum):
    """Severidad de una bandera (§5). Las compuestas ganan a las individuales."""

    AMARILLA = "AMARILLA"
    ROJA = "ROJA"


class EstadoIndicador(StrEnum):
    """Cómo está un indicador respecto a sus umbrales.

    Es la misma evaluación que produce las banderas, expresada por indicador
    en vez de por institución: la ficha de detalle muestra IMOR, ICAP/NICAP e
    ICOR con su semáforo. Se deriva de las reglas de §5.1, nunca de umbrales
    propios, para que la tarjeta y la bandera no puedan contradecirse.
    """

    EN_RANGO = "EN_RANGO"
    ATENCION = "ATENCION"
    """Equivale a una bandera 🟡."""

    ALERTA = "ALERTA"
    """Equivale a una bandera 🔴."""

    SIN_DATO = "SIN_DATO"
    """La CNBV no publica ese indicador para esta figura, o aún no se ingirió."""

    INFORMATIVO = "INFORMATIVO"
    """Tiene valor pero no umbral: es contexto, no señal. La captación, p. ej."""


class UnidadIndicador(StrEnum):
    """Cómo se lee el valor. El formato es cosa de la UI, la unidad no."""

    PORCENTAJE = "PORCENTAJE"
    MONEDA = "MONEDA"
    VECES = "VECES"
    NIVEL = "NIVEL"
    """Categoría prudencial de la CNBV (N1–N4): no es un número."""


class TipoBandera(StrEnum):
    """Qué disparó la bandera. Individuales de §5.1, compuestas de §5.2."""

    IMOR = "IMOR"
    COBERTURA_CARTERA = "COBERTURA_CARTERA"
    ICAP = "ICAP"
    NICAP = "NICAP"
    APALANCAMIENTO = "APALANCAMIENTO"

    # Compuestas (§5.2).
    NO_RECOMENDABLE = "NO_RECOMENDABLE"
    """IMOR alto + ICAP bajo + crecimiento agresivo en captación."""

    RED_FLAG_TASA = "RED_FLAG_TASA"
    """Tasa muy por encima del mercado + IMOR en alerta."""

    GAT_INCONSISTENTE = "GAT_INCONSISTENTE"
    """GAT publicada incoherente con la tasa nominal (> umbral en pp)."""

    SIN_COBERTURA = "SIN_COBERTURA"
    """Informativa y permanente para IFPEs (§5.3)."""


class NivelCapitalizacion(StrEnum):
    """NICAP: categoría prudencial que la CNBV asigna a cada SOFIPO (§5.1).

    No es una métrica de rendimiento — esa es la GAT (§4.3).
    """

    N1 = "N1"
    N2 = "N2"
    N3 = "N3"
    N4 = "N4"


class Liquidez(StrEnum):
    """Facilidad de retiro. Filtro de §7."""

    INMEDIATA = "INMEDIATA"
    AL_VENCIMIENTO = "AL_VENCIMIENTO"
    CON_PENALIZACION = "CON_PENALIZACION"
    """Se puede retirar antes, perdiendo intereses o pagando comisión."""


class EstadoRevision(StrEnum):
    PENDIENTE = "PENDIENTE"
    APROBADA = "APROBADA"
    RECHAZADA = "RECHAZADA"


class EstadoJob(StrEnum):
    EN_CURSO = "EN_CURSO"
    EXITOSO = "EXITOSO"
    FALLIDO = "FALLIDO"
    OMITIDO = "OMITIDO"
    """El job no operó: kill-switch apagado o nada que hacer."""


#: Cobertura de seguro por categoría regulatoria (§4.6). Vive aquí y no en la
#: tabla de instituciones porque es una consecuencia de la figura, no un dato
#: editable por institución.
SEGURO_POR_CATEGORIA: dict[CategoriaInstitucion, TipoSeguro] = {
    CategoriaInstitucion.GOBIERNO: TipoSeguro.SOBERANO,
    CategoriaInstitucion.SOFIPO: TipoSeguro.PROSOFIPO,
    CategoriaInstitucion.BANCO_DIGITAL: TipoSeguro.IPAB,
    CategoriaInstitucion.BANCO_TRADICIONAL: TipoSeguro.IPAB,
    CategoriaInstitucion.IFPE: TipoSeguro.NINGUNO,
}


__all__ = [
    "SEGURO_POR_CATEGORIA",
    "CategoriaInstitucion",
    "EstadoIndicador",
    "EstadoJob",
    "EstadoRevision",
    "EstadoTasa",
    "FuenteTasa",
    "Liquidez",
    "NivelCapitalizacion",
    "Severidad",
    "TipoBandera",
    "TipoInstrumento",
    "TipoProducto",
    "TipoSeguro",
    "UnidadIndicador",
]
