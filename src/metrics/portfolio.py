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

3. **El optimizador es una heurística, no una recomendación.** Cada peso va al
   tramo que más TEN ofrece, hasta donde alcanza el seguro de cada emisor y
   respetando el mínimo de entrada de cada producto. Es transparente y
   reproducible, no óptimo en ningún sentido formal, y así se declara en la
   respuesta y en la página de metodología (§10 y §19).

Todo en `Decimal`. Un reparto calculado en coma flotante acumula error a lo
largo de la cascada y acaba mostrando que la suma no cuadra.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from domain.enums import RazonCorte, RazonDescarte, TipoInstrumento, TipoSeguro
from domain.models import DesgloseCascada, ParametrosFiscales
from metrics.coverage import Cobertura, resolver_cobertura
from metrics.real import desglose_cascada
from metrics.rounding import CENTAVO, redondear
from metrics.ten import ten
from metrics.tramos import Tramo, escalera_de, tasa_ponderada

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
    tramos: tuple[Tramo, ...] = ()
    """Escalera por saldo; vacía = tasa plana. El default preserva a todos los
    llamadores previos al modelo de tramos."""

    @property
    def es_a_la_vista(self) -> bool:
        return self.plazo_dias is None

    @property
    def escalonada(self) -> bool:
        return bool(self.tramos)

    def tasa_aplicada(self, monto: Decimal) -> Decimal:
        """La nominal que este producto paga de verdad a un monto dado."""
        if monto <= 0:
            return self.tasa_nominal
        return tasa_ponderada(monto, escalera_de(self.tasa_nominal, self.tramos))


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
class PasoOptimizacion:
    """Una vuelta del water-filling: dónde fue el dinero y qué lo detuvo."""

    producto_id: int
    indice_tramo: int
    """Posición del tramo en la escalera efectiva al momento de asignar."""

    tramo: Tramo
    ten_marginal: Decimal
    """La oferta que ganó la vuelta. En una apertura con mínimo es la TEN de
    la ponderada del mínimo — el costo real de entrar, no el del tramo alto."""

    monto: Decimal
    """Redondeado a centavo, como las asignaciones. Con montos enteros de la
    UI la suma de los pasos de un producto es exactamente su asignación."""

    razon_corte: RazonCorte
    compra_minimo: bool
    """La apertura compró el mínimo cruzando tramos. Va aparte de la razón:
    puede coincidir con cualquier corte si los topes empatan al centavo."""


@dataclass(frozen=True, slots=True)
class Descarte:
    """Un producto que quedó fuera del reparto automático, y por qué."""

    producto_id: int
    razon: RazonDescarte


@dataclass(frozen=True, slots=True)
class Reparto:
    """Lo que el optimizador propone, en pesos."""

    asignaciones: list[tuple[Candidato, Decimal]]
    monto_no_asignado: Decimal
    """Lo que no cupo: sin emisor sin tope, la cobertura disponible se agota."""

    pasos: list[PasoOptimizacion]
    """El llenado, vuelta a vuelta: es la explicación del reparto."""

    descartes: list[Descarte]
    """Sólo productos que no recibieron ni un peso: lo fondeado cuenta su
    historia en la razón de corte de sus pasos."""

    @property
    def candidatos(self) -> list[Candidato]:
        return [c for c, _ in self.asignaciones]

    @property
    def montos(self) -> list[Decimal]:
        return [m for _, m in self.asignaciones]


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
    """Evalúa un reparto expresado en porcentajes. El camino del usuario."""
    if monto_total <= 0:
        raise ValueError("el monto total debe ser positivo")
    if len(candidatos) != len(porcentajes):
        raise ValueError("hay que dar un porcentaje por candidato")

    # Sin instrumentos no hay caso especial: `normalizar` devuelve una lista
    # vacía, el bucle no itera y los agregados salen en cero por sí solos.
    # Construir aquí una respuesta de ceros a mano sería un segundo camino que
    # habría que mantener en sincronía con el primero.
    return evaluar_reparto(
        candidatos,
        _montos(monto_total, normalizar(porcentajes)),
        horizonte_dias=horizonte_dias,
        inflacion_anual=inflacion_anual,
        params=params,
        valor_udi=valor_udi,
    )


def evaluar_reparto(
    candidatos: Sequence[Candidato],
    montos: Sequence[Decimal],
    *,
    horizonte_dias: int,
    inflacion_anual: Decimal,
    params: ParametrosFiscales,
    valor_udi: Decimal,
) -> Combinacion:
    """Evalúa un reparto expresado en pesos. El camino del optimizador.

    Existe separado de `evaluar_combinacion` por una razón concreta: el
    optimizador calcula importes exactos que respetan cada tope al centavo, y
    convertirlos a porcentajes de un decimal para volver a convertirlos a
    importes los mueve. Sobre $5,000,000 un decimal de porcentaje son $5,000,
    suficiente para colocar a una institución **por encima de su cobertura**
    justo después de haberla respetado. El único reparto que no pierde nada al
    redondear es el que nunca se expresa en porcentajes.
    """
    if horizonte_dias <= 0:
        raise ValueError("el horizonte debe ser positivo")
    if len(candidatos) != len(montos):
        raise ValueError("hay que dar un monto por candidato")

    monto_total = sum(montos, Decimal("0"))
    porcentajes = [
        redondear(m * CIEN / monto_total, PORCENTAJE_REPARTO) if monto_total > 0 else Decimal("0")
        for m in montos
    ]

    # Lo que ya consumió cada institución de su propio tope. La clave es la
    # institución y no el producto: ahí está la regla que hace que la suma sea
    # correcta cuando alguien reparte entre dos productos del mismo emisor.
    usado_por_institucion: dict[int, Decimal] = {}

    asignaciones: list[Asignacion] = []
    for candidato, porcentaje, monto in zip(candidatos, porcentajes, montos, strict=True):
        cobertura = resolver_cobertura(candidato.tipo_seguro, valor_udi)
        previo = usado_por_institucion.get(candidato.institucion_id, Decimal("0"))

        if cobertura.sin_limite:
            cubierto = monto
        else:
            disponible = max((cobertura.limite_mxn or Decimal("0")) - previo, Decimal("0"))
            cubierto = min(monto, disponible)
        usado_por_institucion[candidato.institucion_id] = previo + monto

        # La nominal que este monto gana de verdad: en un producto escalonado
        # es la ponderada de su escalera, en uno plano es la titular. TEN y
        # cascada salen de ella para que lo mostrado cuadre con los importes.
        aplicada = candidato.tasa_aplicada(monto)

        asignaciones.append(
            Asignacion(
                candidato=candidato,
                porcentaje=porcentaje,
                monto=monto,
                ten=ten(aplicada, candidato.instrumento, params),
                cascada=desglose_cascada(
                    monto=monto,
                    tasa_nominal=aplicada,
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
        porcentaje_protegido=(
            (protegido * CIEN / monto_total).to_integral_value(rounding="ROUND_FLOOR")
            if monto_total > 0
            else Decimal("0")
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


def _razon_no_elegible(
    candidato: Candidato,
    *,
    monto_total: Decimal,
    horizonte_dias: int,
    excluir_rojas: bool,
    solo_vista: bool = False,
) -> RazonDescarte | None:
    """La primera condición de elegibilidad que falla, o `None` si entra.

    El orden es el del predicado histórico de `elegibles` — plazo, mínimo,
    bandera — para que la razón reportada no dependa de una reordenación
    accidental de las condiciones. La comprobación de `solo_vista` va antes
    que todas: en modo vista el horizonte sigue siendo el periodo de
    proyección (un plazo de 91 días cabe en 364), así que reportar
    `PLAZO_MAYOR_AL_HORIZONTE` mentiría — el hecho que descalifica es tener
    plazo, no excederse de él.
    """
    if solo_vista and not candidato.es_a_la_vista:
        return RazonDescarte.TIENE_PLAZO
    if not (candidato.es_a_la_vista or (candidato.plazo_dias or 0) <= horizonte_dias):
        return RazonDescarte.PLAZO_MAYOR_AL_HORIZONTE
    if candidato.monto_minimo > monto_total:
        return RazonDescarte.MINIMO_SUPERA_MONTO
    if excluir_rojas and candidato.tiene_bandera_roja:
        return RazonDescarte.BANDERA_ROJA
    return None


def elegibles(
    candidatos: Sequence[Candidato],
    *,
    monto_total: Decimal,
    horizonte_dias: int,
    excluir_rojas: bool,
    solo_vista: bool = False,
) -> list[Candidato]:
    """Qué puede entrar en un reparto automático.

    Se descarta lo que el usuario no podría contratar (monto mínimo por encima
    de su capital) y lo que no vence dentro de su horizonte. Un plazo que no
    cabe puede elegirse a mano —con su advertencia— pero no lo propone la
    herramienta: proponerlo sería recomendar iliquidez sin decirlo. Con
    `solo_vista`, además, sólo entran productos de liquidez inmediata: el
    dinero que debe estar siempre disponible no se propone a plazo.
    """
    return [
        c
        for c in candidatos
        if _razon_no_elegible(
            c,
            monto_total=monto_total,
            horizonte_dias=horizonte_dias,
            excluir_rojas=excluir_rojas,
            solo_vista=solo_vista,
        )
        is None
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
    solo_vista: bool = False,
) -> Reparto:
    """Reparto por tramos: cada peso al segmento que más TEN ofrece.

    Con `solo_vista`, el reparto se restringe a productos de liquidez
    inmediata (los a plazo se descartan con `TIENE_PLAZO`); el horizonte
    sigue siendo el periodo de proyección del rendimiento.

    Devuelve **importes**, no porcentajes. Sigue siendo una heurística
    deliberadamente simple y explicable —"el dinero va al tramo que más
    rinde, hasta donde alcanza el seguro de cada emisor"— pero consciente de
    las escaleras: el 13% de Openbank solo existe para los primeros $30,000,
    y una vez llenos la siguiente mejor oferta puede estar en otro emisor. El
    puntero de segmento por producto garantiza estructuralmente que jamás se
    asigna al tramo i+1 sin llenar el i. Con candidatos planos degrada
    exactamente al greedy por TEN de siempre.

    Reglas que conserva, y dos que estrena:

    - Un producto por institución: dos del mismo emisor comparten tope, así
      que añadir el segundo no protege más dinero y sólo complica el reparto.
    - Puede quedar dinero sin colocar y el remanente se declara en vez de
      repartirse entre los que ya están llenos.
    - **El mínimo de entrada se respeta**: un producto no se abre si el
      dinero disponible —o el tope de su emisor— no alcanza su
      `monto_minimo`; proponer una asignación por debajo del mínimo es
      proponer algo incontratable. Y su oferta de entrada es la TEN efectiva
      de ese mínimo, no la del primer tramo: entrar a un producto con mínimo
      de $50,000 y tramo alto de $30,000 obliga a comprar también el bajo.
    - Las escaleras con algún escalón **creciente** quedan fuera del
      optimizador (siguen en el comparador y la combinación manual): el
      greedy marginal solo es óptimo con escaleras no crecientes, y esta
      respuesta promete una heurística que se explica en una frase.
    """
    if monto_total <= 0:
        raise ValueError("el monto total debe ser positivo")

    aptos: list[Candidato] = []
    escaleras: dict[int, tuple[Tramo, ...]] = {}
    razones_descarte: dict[int, RazonDescarte] = {}
    for candidato in candidatos:
        razon = _razon_no_elegible(
            candidato,
            monto_total=monto_total,
            horizonte_dias=horizonte_dias,
            excluir_rojas=excluir_rojas,
            solo_vista=solo_vista,
        )
        if razon is not None:
            razones_descarte[candidato.producto_id] = razon
            continue
        escalera = escalera_de(candidato.tasa_nominal, candidato.tramos)
        crece = any(
            siguiente.tasa_nominal > tramo.tasa_nominal
            for tramo, siguiente in zip(escalera, escalera[1:], strict=False)
        )
        if crece:
            razones_descarte[candidato.producto_id] = RazonDescarte.ESCALERA_CRECIENTE
            continue
        aptos.append(candidato)
        escaleras[candidato.producto_id] = escalera

    # Orden estable de exploración: con ofertas empatadas gana el producto de
    # id menor, el mismo desempate de siempre.
    aptos.sort(key=lambda c: c.producto_id)

    restante = monto_total
    puntero: dict[int, int] = {c.producto_id: 0 for c in aptos}
    acumulado: dict[int, Decimal] = {c.producto_id: Decimal("0") for c in aptos}
    abierta_por: dict[int, int] = {}
    descartados: set[int] = set()
    orden_apertura: list[Candidato] = []
    pasos: list[PasoOptimizacion] = []

    def _tope_restante(candidato: Candidato) -> Decimal:
        """Cuánto más admite el emisor de este producto sin exponer dinero."""
        if not respetar_seguro:
            return restante
        cobertura = resolver_cobertura(candidato.tipo_seguro, valor_udi)
        if cobertura.sin_limite:
            return restante
        limite = cobertura.limite_mxn or Decimal("0")
        return max(limite - acumulado[candidato.producto_id], Decimal("0"))

    while restante > 0:
        mejor: Candidato | None = None
        mejor_oferta: Decimal | None = None
        for candidato in aptos:
            pid = candidato.producto_id
            if pid in descartados or puntero[pid] >= len(escaleras[pid]):
                continue
            duena = abierta_por.get(candidato.institucion_id)
            if duena is not None and duena != pid:
                continue
            tope = _tope_restante(candidato)
            if tope <= 0:
                # Emisor lleno (o sin cobertura con el seguro activo, el caso
                # del IFPE): este producto ya no volverá a ofertar.
                descartados.add(pid)
                razones_descarte[pid] = (
                    RazonDescarte.SIN_COBERTURA
                    if resolver_cobertura(candidato.tipo_seguro, valor_udi).sin_cobertura
                    else RazonDescarte.EMISOR_LLENO
                )
                continue
            if duena is None and min(restante, tope) < candidato.monto_minimo:
                descartados.add(pid)
                razones_descarte[pid] = RazonDescarte.MINIMO_INALCANZABLE
                continue
            if duena == pid:
                marginal = escaleras[pid][puntero[pid]].tasa_nominal
            elif candidato.monto_minimo > 0:
                marginal = tasa_ponderada(candidato.monto_minimo, escaleras[pid])
            else:
                marginal = escaleras[pid][0].tasa_nominal
            oferta = ten(marginal, candidato.instrumento, params)
            if mejor_oferta is None or oferta > mejor_oferta:
                mejor, mejor_oferta = candidato, oferta

        if mejor is None or mejor_oferta is None:
            break

        pid = mejor.producto_id
        escalera = escaleras[pid]
        indice_tramo = puntero[pid]
        tramo = escalera[indice_tramo]
        tope = _tope_restante(mejor)
        capacidad = restante if tramo.hasta is None else tramo.hasta - acumulado[pid]
        es_apertura = abierta_por.get(mejor.institucion_id) != pid

        if es_apertura:
            # La apertura compra al menos el mínimo, cruzando tramos si hace
            # falta: ya se comprobó arriba que el mínimo cabe.
            asignado = min(restante, tope, max(capacidad, mejor.monto_minimo))
            abierta_por[mejor.institucion_id] = pid
            orden_apertura.append(mejor)
        else:
            asignado = min(restante, tope, capacidad)

        # La razón del corte, con la consecuencia más fuerte primero (ver
        # `RazonCorte`): con empates al centavo, quedarse sin monto gana al
        # límite de seguro, y éste a la compra del mínimo. Dos empates
        # estructurales se resuelven solos: sin `respetar_seguro` el tope ES
        # `restante`, así que jamás se reporta «límite de seguro»; y en un
        # tramo sin techo la capacidad ES `restante`, así que el corte que se
        # reporta es el monto agotándose, no un tramo que no existe.
        compra_minimo = es_apertura and mejor.monto_minimo > capacidad
        if asignado == restante:
            razon_corte = RazonCorte.MONTO_AGOTADO
        elif asignado == tope:
            razon_corte = RazonCorte.LIMITE_SEGURO
        elif compra_minimo and asignado == mejor.monto_minimo:
            razon_corte = RazonCorte.COMPRA_MINIMO
        else:
            razon_corte = RazonCorte.TRAMO_LLENO

        pasos.append(
            PasoOptimizacion(
                producto_id=pid,
                indice_tramo=indice_tramo,
                tramo=tramo,
                ten_marginal=mejor_oferta,
                monto=redondear(asignado, CENTAVO),
                razon_corte=razon_corte,
                compra_minimo=compra_minimo,
            )
        )

        acumulado[pid] += asignado
        restante -= asignado
        while puntero[pid] < len(escalera):
            lleno = escalera[puntero[pid]]
            if lleno.hasta is not None and acumulado[pid] >= lleno.hasta:
                puntero[pid] += 1
            else:
                break

    # Un descarte es un producto que no recibió ni un peso: el que llenó su
    # tope sí está en el reparto y su historia la cuenta el corte de su
    # último paso, no esta lista.
    fondeados = {pid for pid, monto in acumulado.items() if monto > 0}
    return Reparto(
        asignaciones=[
            (candidato, redondear(acumulado[candidato.producto_id], CENTAVO))
            for candidato in orden_apertura
        ],
        monto_no_asignado=redondear(max(restante, Decimal("0")), CENTAVO),
        pasos=pasos,
        descartes=[
            Descarte(producto_id=pid, razon=razon)
            for pid, razon in sorted(razones_descarte.items())
            if pid not in fondeados
        ],
    )


# ─── Referencias de comparación ───────────────────────────────


def referencia_cetes(
    candidatos: Sequence[Candidato],
    *,
    monto_total: Decimal,
    horizonte_dias: int,
    excluir_rojas: bool,
    solo_vista: bool = False,
) -> Candidato | None:
    """El CETES contra el que comparar: el de mayor plazo que cabe.

    Se elige del mismo pool de `elegibles` que usa el optimizador — así
    respeta su mínimo de contratación y su plazo — y gana el de mayor
    `plazo_dias` (la vista cuenta como 0); empate → menor `producto_id`. Sin
    candidato, la referencia se omite: no se inventa un CETES que el catálogo
    no tiene. Con `solo_vista` todo CETES es a plazo, así que la referencia
    desaparece por el mismo mecanismo — comparar contra un instrumento que el
    modo excluye describiría mal la pregunta que el usuario hizo.
    """
    pool = [
        c
        for c in elegibles(
            candidatos,
            monto_total=monto_total,
            horizonte_dias=horizonte_dias,
            excluir_rojas=excluir_rojas,
            solo_vista=solo_vista,
        )
        if c.instrumento is TipoInstrumento.CETES
    ]
    if not pool:
        return None
    return min(pool, key=lambda c: (-(c.plazo_dias or 0), c.producto_id))


def mejor_unico(
    candidatos: Sequence[Candidato],
    *,
    monto_total: Decimal,
    horizonte_dias: int,
    inflacion_anual: Decimal,
    params: ParametrosFiscales,
    valor_udi: Decimal,
    excluir_rojas: bool,
    solo_vista: bool = False,
) -> tuple[Candidato, Combinacion] | None:
    """«Todo en un solo instrumento»: el elegible con mayor ganancia real.

    Evalúa cada elegible con el monto entero y toma el argmax de
    `ganancia_real`; empate → menor `producto_id`. Dos decisiones
    deliberadas, que también documenta el schema de la API:

    - **No se filtra por `respetar_seguro`**: la referencia lleva su propio
      porcentaje protegido, y ocultar el instrumento de mayor ganancia
      describiría mal el mercado. Que «todo en un IFPE» aparezca con
      protegido 0 % junto a una combinación 100 % cubierta es el porqué de
      diversificar, hecho visible.
    - **Las escaleras crecientes sí entran**: son contratables y
      `evaluar_reparto` las pondera bien; su exclusión es sólo del greedy.
    """
    pool = elegibles(
        candidatos,
        monto_total=monto_total,
        horizonte_dias=horizonte_dias,
        excluir_rojas=excluir_rojas,
        solo_vista=solo_vista,
    )
    mejor: tuple[Candidato, Combinacion] | None = None
    for candidato in sorted(pool, key=lambda c: c.producto_id):
        combinacion = evaluar_reparto(
            [candidato],
            [monto_total],
            horizonte_dias=horizonte_dias,
            inflacion_anual=inflacion_anual,
            params=params,
            valor_udi=valor_udi,
        )
        if mejor is None or combinacion.ganancia_real > mejor[1].ganancia_real:
            mejor = (candidato, combinacion)
    return mejor


__all__ = [
    "CIEN",
    "PORCENTAJE_REPARTO",
    "Asignacion",
    "Candidato",
    "Combinacion",
    "Descarte",
    "PasoOptimizacion",
    "Reparto",
    "elegibles",
    "evaluar_combinacion",
    "evaluar_reparto",
    "mejor_unico",
    "normalizar",
    "optimizar",
    "referencia_cetes",
]
