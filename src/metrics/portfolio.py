"""Combinación de instrumentos y optimizador de reparto.

La calculadora del producto no evalúa un instrumento sino un **reparto** entre
varios: cuánto rinde de verdad poner 40% en CETES y 60% en una SOFIPO, y
cuánto de ese dinero queda protegido. Este módulo hace ese cálculo y propone
un reparto automático.

Tres cosas que decide aquí y no en el frontend, porque son de dinero:

1. **El tope de cobertura es por institución, no por producto.** Dos productos
   de la misma SOFIPO comparten los 25,000 UDIs de PROSOFIPO. Tratarlos por
   separado duplicaría la protección sobre el papel y le diría al usuario que
   está cubierto cuando no lo está — el error más caro que puede cometer esta
   herramienta.

2. **El porcentaje protegido se trunca hacia abajo.** Si sobra un peso sin
   cubrir, la respuesta es 99%, no 100%. Redondear al alza convertiría un
   excedente pequeño en una promesa completa.

3. **El optimizador es una heurística, no una recomendación.** Ordena por TEN
   y llena hasta el tope de cada emisor. Es transparente y reproducible, no
   óptimo en ningún sentido formal, y así se declara en la respuesta y en la
   página de metodología (§10 y §19).

Todo en `Decimal`. Un reparto calculado en coma flotante acumula error a lo
largo de la cascada y acaba mostrando que la suma no cuadra.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from domain.enums import TipoInstrumento, TipoSeguro
from domain.models import DesgloseCascada, ParametrosFiscales
from metrics.coverage import Cobertura, resolver_cobertura
from metrics.real import desglose_cascada
from metrics.rounding import CENTAVO, redondear
from metrics.ten import ten

#: Los porcentajes del reparto se manejan con un decimal, como en la UI. Más
#: precisión no significaría nada: el usuario los teclea en un campo con
#: `step="0.1"`.
PORCENTAJE_REPARTO = Decimal("0.1")

CIEN = Decimal("100")


@dataclass(frozen=True, slots=True)
class Candidato:
    """Un producto con lo que hace falta para repartir monto sobre él."""

    producto_id: int
    institucion_id: int
    tipo_seguro: TipoSeguro
    instrumento: TipoInstrumento
    tasa_nominal: Decimal
    plazo_dias: int | None
    """`None` es un producto a la vista: disponible en cualquier horizonte."""

    monto_minimo: Decimal
    tiene_bandera_roja: bool = False

    @property
    def es_a_la_vista(self) -> bool:
        return self.plazo_dias is None


@dataclass(frozen=True, slots=True)
class Asignacion:
    """Lo que le toca a un instrumento dentro del reparto."""

    candidato: Candidato
    porcentaje: Decimal
    monto: Decimal
    ten: Decimal
    cascada: DesgloseCascada
    cobertura: Cobertura
    monto_cubierto: Decimal
    monto_expuesto: Decimal
    advertencia_liquidez: str | None

    @property
    def cubierto(self) -> bool:
        return self.monto_expuesto == 0


@dataclass(frozen=True, slots=True)
class Combinacion:
    """El reparto entero, con su cascada agregada."""

    monto_total: Decimal
    horizonte_dias: int
    ten_ponderada: Decimal
    rendimiento_bruto: Decimal
    isr_retenido: Decimal
    rendimiento_neto: Decimal
    efecto_inflacion: Decimal
    ganancia_real: Decimal
    monto_protegido: Decimal
    porcentaje_protegido: Decimal
    asignaciones: list[Asignacion]


# ─── Normalización de porcentajes ─────────────────────────────


def normalizar(pesos: Sequence[Decimal]) -> list[Decimal]:
    """Escala los pesos para que sumen exactamente 100.

    El usuario teclea porcentajes a mano y casi nunca suman 100. En vez de
    rechazar la entrada, se normaliza: lo que expresa es una proporción, y el
    reparto de $250,000 entre "70 y 40" es el mismo que entre "63.6 y 36.4".

    El residuo del redondeo va al primer instrumento. Repartirlo daría un
    reparto distinto según el orden de la lista, y con un decimal de precisión
    el residuo nunca pasa de unas décimas.
    """
    if not pesos:
        return []

    total = sum(pesos, Decimal(0))
    if total <= 0:
        # Sin pesos útiles, reparto igual: es lo que el usuario espera al
        # añadir instrumentos sin tocar los porcentajes.
        iguales = [redondear(CIEN / len(pesos), PORCENTAJE_REPARTO) for _ in pesos]
        return _cuadrar(iguales)

    return _cuadrar([redondear(p * CIEN / total, PORCENTAJE_REPARTO) for p in pesos])


def _cuadrar(porcentajes: list[Decimal]) -> list[Decimal]:
    residuo = CIEN - sum(porcentajes, Decimal(0))
    if residuo:
        porcentajes[0] = redondear(porcentajes[0] + residuo, PORCENTAJE_REPARTO)
    return porcentajes


def _montos(monto_total: Decimal, porcentajes: Sequence[Decimal]) -> list[Decimal]:
    """Reparte el monto en centavos exactos.

    El último se lleva el remanente en vez de calcularse: con porcentajes
    redondeados, la suma de las partes puede quedar a un centavo del total, y
    ese centavo tiene que estar en algún sitio para que el detalle cuadre con
    el encabezado.
    """
    if not porcentajes:
        return []
    montos = [redondear(monto_total * p / CIEN, CENTAVO) for p in porcentajes[:-1]]
    montos.append(redondear(monto_total - sum(montos, Decimal(0)), CENTAVO))
    return montos


# ─── Evaluación de un reparto ─────────────────────────────────


def evaluar_combinacion(
    candidatos: Sequence[Candidato],
    porcentajes: Sequence[Decimal],
    *,
    monto_total: Decimal,
    horizonte_dias: int,
    inflacion_anual: Decimal,
    params: ParametrosFiscales,
    valor_udi: Decimal,
) -> Combinacion:
    """Calcula el reparto completo: cascada agregada y protección real."""
    if monto_total <= 0:
        raise ValueError("el monto total debe ser positivo")
    if horizonte_dias <= 0:
        raise ValueError("el horizonte debe ser positivo")
    if len(candidatos) != len(porcentajes):
        raise ValueError("hay que dar un porcentaje por candidato")

    # Sin instrumentos no hay caso especial: `normalizar` devuelve una lista
    # vacía, el bucle no itera y los agregados salen en cero por sí solos.
    # Construir aquí una respuesta de ceros a mano sería un segundo camino que
    # habría que mantener en sincronía con el primero.
    normalizados = normalizar(porcentajes)
    montos = _montos(monto_total, normalizados)

    # Lo que ya consumió cada institución de su propio tope. La clave es la
    # institución y no el producto: ahí está la regla que hace que la suma sea
    # correcta cuando alguien reparte entre dos productos del mismo emisor.
    usado_por_institucion: dict[int, Decimal] = {}

    asignaciones: list[Asignacion] = []
    for candidato, porcentaje, monto in zip(candidatos, normalizados, montos, strict=True):
        cobertura = resolver_cobertura(candidato.tipo_seguro, valor_udi)
        previo = usado_por_institucion.get(candidato.institucion_id, Decimal("0"))

        if cobertura.sin_limite:
            cubierto = monto
        else:
            disponible = max((cobertura.limite_mxn or Decimal("0")) - previo, Decimal("0"))
            cubierto = min(monto, disponible)
        usado_por_institucion[candidato.institucion_id] = previo + monto

        asignaciones.append(
            Asignacion(
                candidato=candidato,
                porcentaje=porcentaje,
                monto=monto,
                ten=ten(candidato.tasa_nominal, candidato.instrumento, params),
                cascada=desglose_cascada(
                    monto=monto,
                    tasa_nominal=candidato.tasa_nominal,
                    instrumento=candidato.instrumento,
                    plazo_dias=horizonte_dias,
                    inflacion_anual=inflacion_anual,
                    params=params,
                ),
                cobertura=cobertura,
                monto_cubierto=redondear(cubierto, CENTAVO),
                monto_expuesto=redondear(monto - cubierto, CENTAVO),
                advertencia_liquidez=_advertencia_liquidez(candidato, horizonte_dias),
            )
            if monto > 0
            else _asignacion_vacia(candidato, porcentaje, cobertura, params, horizonte_dias)
        )

    protegido = sum((a.monto_cubierto for a in asignaciones), Decimal("0"))

    return Combinacion(
        monto_total=redondear(monto_total, CENTAVO),
        horizonte_dias=horizonte_dias,
        ten_ponderada=_ten_ponderada(asignaciones),
        rendimiento_bruto=_suma(asignaciones, "rendimiento_bruto"),
        isr_retenido=_suma(asignaciones, "isr_retenido"),
        rendimiento_neto=_suma(asignaciones, "rendimiento_neto"),
        efecto_inflacion=_suma(asignaciones, "efecto_inflacion"),
        ganancia_real=_suma(asignaciones, "ganancia_real"),
        monto_protegido=redondear(protegido, CENTAVO),
        # Truncado, no redondeado: si sobra un peso sin cubrir la respuesta es
        # 99%, nunca 100%.
        porcentaje_protegido=(protegido * CIEN / monto_total).to_integral_value(
            rounding="ROUND_FLOOR"
        ),
        asignaciones=asignaciones,
    )


def _asignacion_vacia(
    candidato: Candidato,
    porcentaje: Decimal,
    cobertura: Cobertura,
    params: ParametrosFiscales,
    horizonte_dias: int,
) -> Asignacion:
    """Un instrumento con 0% sigue en la lista, con todo a cero.

    `desglose_cascada` rechaza montos no positivos —y hace bien—, pero quitar
    la fila de la respuesta haría desaparecer de la pantalla un instrumento
    que el usuario sí seleccionó.
    """
    cero = Decimal("0.00")
    return Asignacion(
        candidato=candidato,
        porcentaje=porcentaje,
        monto=cero,
        ten=ten(candidato.tasa_nominal, candidato.instrumento, params),
        cascada=DesgloseCascada(
            monto_invertido=cero,
            rendimiento_bruto=cero,
            isr_retenido=cero,
            rendimiento_neto=cero,
            efecto_inflacion=cero,
            ganancia_real=cero,
            plazo_dias=horizonte_dias,
            tasa_nominal=candidato.tasa_nominal,
            ten=ten(candidato.tasa_nominal, candidato.instrumento, params),
            inflacion_anual=Decimal("0"),
            nota_fiscal="",
        ),
        cobertura=cobertura,
        monto_cubierto=cero,
        monto_expuesto=cero,
        advertencia_liquidez=_advertencia_liquidez(candidato, horizonte_dias),
    )


def _advertencia_liquidez(candidato: Candidato, horizonte_dias: int) -> str | None:
    """El plazo del producto no cabe en el horizonte del usuario.

    El cálculo se hace igual —el usuario pidió ese horizonte— pero el número
    supone que puede disponer del dinero, y en un plazo fijo no puede. Sin este
    aviso la comparación favorece silenciosamente a los plazos más largos.
    """
    if candidato.es_a_la_vista or candidato.plazo_dias is None:
        return None
    if candidato.plazo_dias <= horizonte_dias:
        return None
    return (
        f"Este producto vence a {candidato.plazo_dias} días, después del horizonte de "
        f"{horizonte_dias} que elegiste. El rendimiento mostrado supone que mantienes "
        f"la inversión hasta el vencimiento."
    )


def _suma(asignaciones: Sequence[Asignacion], campo: str) -> Decimal:
    total: Decimal = sum((getattr(a.cascada, campo) for a in asignaciones), Decimal("0"))
    return redondear(total, CENTAVO)


def _ten_ponderada(asignaciones: Sequence[Asignacion]) -> Decimal:
    """TEN media ponderada por monto, no por número de instrumentos."""
    total = sum((a.monto for a in asignaciones), Decimal("0"))
    if total <= 0:
        return Decimal("0.0000")
    ponderada = sum((a.ten * a.monto for a in asignaciones), Decimal("0")) / total
    return redondear(ponderada, Decimal("0.0001"))


# ─── Optimizador ──────────────────────────────────────────────


def elegibles(
    candidatos: Sequence[Candidato],
    *,
    monto_total: Decimal,
    horizonte_dias: int,
    excluir_rojas: bool,
) -> list[Candidato]:
    """Qué puede entrar en un reparto automático.

    Se descarta lo que el usuario no podría contratar (monto mínimo por encima
    de su capital) y lo que no vence dentro de su horizonte. Un plazo que no
    cabe puede elegirse a mano —con su advertencia— pero no lo propone la
    herramienta: proponerlo sería recomendar iliquidez sin decirlo.
    """
    return [
        c
        for c in candidatos
        if (c.es_a_la_vista or (c.plazo_dias or 0) <= horizonte_dias)
        and c.monto_minimo <= monto_total
        and not (excluir_rojas and c.tiene_bandera_roja)
    ]


def optimizar(
    candidatos: Sequence[Candidato],
    *,
    monto_total: Decimal,
    horizonte_dias: int,
    params: ParametrosFiscales,
    valor_udi: Decimal,
    respetar_seguro: bool = True,
    excluir_rojas: bool = True,
) -> list[tuple[Candidato, Decimal]]:
    """Reparto greedy por TEN, respetando el tope de cada emisor.

    Devuelve pares (candidato, porcentaje). Es una heurística deliberadamente
    simple y explicable: el usuario tiene que poder entender por qué le
    propone lo que le propone, y "el que más rinde primero, hasta donde
    alcanza el seguro" se explica en una frase.

    Un producto por institución: dos del mismo emisor comparten tope, así que
    añadir el segundo no protege más dinero y sólo complica el reparto.
    """
    if monto_total <= 0:
        raise ValueError("el monto total debe ser positivo")

    disponibles = sorted(
        elegibles(
            candidatos,
            monto_total=monto_total,
            horizonte_dias=horizonte_dias,
            excluir_rojas=excluir_rojas,
        ),
        key=lambda c: (
            -ten(c.tasa_nominal, c.instrumento, params),
            c.producto_id,  # desempate estable: el mismo orden en cada llamada
        ),
    )

    restante = monto_total
    usados: set[int] = set()
    reparto: list[tuple[Candidato, Decimal]] = []

    for candidato in disponibles:
        if restante <= 0:
            break
        if candidato.institucion_id in usados:
            continue

        if respetar_seguro:
            cobertura = resolver_cobertura(candidato.tipo_seguro, valor_udi)
            tope = restante if cobertura.sin_limite else (cobertura.limite_mxn or Decimal("0"))
        else:
            tope = restante

        asignado = min(restante, tope)
        if asignado <= 0:
            # Sin cobertura y con el seguro activo: no entra. Es el caso de un
            # IFPE, y omitirlo en silencio es lo correcto — el optimizador no
            # debe proponer dinero sin proteger cuando le pidieron protegerlo.
            continue

        reparto.append((candidato, asignado))
        usados.add(candidato.institucion_id)
        restante -= asignado

    if not reparto:
        return []

    porcentajes = normalizar([monto for _, monto in reparto])
    return [(candidato, pct) for (candidato, _), pct in zip(reparto, porcentajes, strict=True)]


__all__ = [
    "CIEN",
    "PORCENTAJE_REPARTO",
    "Asignacion",
    "Candidato",
    "Combinacion",
    "elegibles",
    "evaluar_combinacion",
    "normalizar",
    "optimizar",
]
