"""Tests del reviewer: un caso por celda de la tabla de decisión.

Contra Postgres real porque lo que se verifica es qué filas quedan escritas —
en `tasas` y en `revisiones_tasas`— y con qué estado.

El caso que más importa es el de la primera lectura: aunque coincida al centavo
con lo que decía el agregador, va a revisión. Coincidir con un dato sin
verificar no lo verifica.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from cli.seed import run_seed
from core.db import session_scope
from domain.enums import EstadoRevision, EstadoTasa, FuenteTasa, TipoProducto
from domain.orm import Producto, RevisionTasa, Tasa, TramoTasa
from rates_agent.escalera import EscaleraExtraida, reconstruir_escalera
from rates_agent.extractor import TasaExtraida
from rates_agent.reviewer import Decision, HuecoCatalogo, ReporteRevision, Resultado, revisar

pytestmark = [pytest.mark.requires_docker, pytest.mark.usefixtures("real_db", "real_redis")]

URL = "https://finsus.test/inversion"


def _extraida(tasa: str, *, confianza: str = "alta", plazo: int | None = 364) -> TasaExtraida:
    return TasaExtraida(
        producto="Plazo fijo",
        tipo=TipoProducto.PLAZO if plazo else TipoProducto.VISTA,
        plazo_dias=plazo,
        tasa_nominal=Decimal(tasa),
        confianza=confianza,  # type: ignore[arg-type]
    )


async def _producto(slug: str = "finsus-plazo-364") -> Producto:
    async with session_scope() as session:
        producto = await session.scalar(select(Producto).where(Producto.slug == slug))
    assert producto is not None
    return producto


def _tasa(
    producto_id: int,
    valor: str,
    *,
    fuente: FuenteTasa,
    estado: EstadoTasa,
    tramos: list[TramoTasa] | None = None,
) -> Tasa:
    # `tramos` se inicializa SIEMPRE, aunque sea vacío: el reviewer compara la
    # escalera de la vigente, y sobre un objeto recién flusheado —no venido de
    # un select, que es el camino de producción— el primer acceso a una
    # colección sin cargar dispararía un lazy load que bajo async revienta.
    return Tasa(
        producto_id=producto_id,
        tasa_nominal=Decimal(valor),
        fecha_dato=date.today() - timedelta(days=7),
        fuente=fuente,
        estado=estado,
        tramos=tramos or [],
    )


def _escalera_openbank(alta: str = "13.00", baja: str = "6.30") -> EscaleraExtraida:
    """La escalera canónica, reconstruida como lo haría el pipeline."""
    entradas = [
        TasaExtraida(
            producto="Plazo fijo",
            tipo=TipoProducto.PLAZO,
            plazo_dias=364,
            tasa_nominal=Decimal(alta),
            monto_minimo=Decimal("0"),
        ),
        TasaExtraida(
            producto="Plazo fijo",
            tipo=TipoProducto.PLAZO,
            plazo_dias=364,
            tasa_nominal=Decimal(baja),
            monto_minimo=Decimal("30000"),
        ),
    ]
    escalera = reconstruir_escalera(entradas)
    assert escalera is not None
    return escalera


def _tramos_orm(alta: str = "13.00", baja: str = "6.30") -> list[TramoTasa]:
    return [
        TramoTasa(desde=Decimal("0"), hasta=Decimal("30000"), tasa_nominal=Decimal(alta)),
        TramoTasa(desde=Decimal("30000"), hasta=None, tasa_nominal=Decimal(baja)),
    ]


async def _contar(modelo: type) -> int:
    async with session_scope() as session:
        return int(await session.scalar(select(func.count()).select_from(modelo)) or 0)


# ─── Primera lectura ──────────────────────────────────────────


async def test_the_first_official_reading_always_goes_to_review() -> None:
    """Aunque coincida exactamente con el agregador."""
    await run_seed()
    producto = await _producto()

    async with session_scope() as session:
        agregador = _tasa(
            producto.id, "8.69", fuente=FuenteTasa.AGREGADOR, estado=EstadoTasa.PENDIENTE_REVISION
        )
        session.add(agregador)
        await session.flush()
        resultado = await revisar(
            session,
            _extraida("8.69"),
            producto=producto,
            vigente=None,
            referencia=agregador,
            url=URL,
        )

    assert resultado.decision is Decision.EN_REVISION
    assert "Primera lectura oficial" in resultado.motivo
    # El motivo lleva el contraste enfrente para quien revise.
    assert "AGREGADOR" in resultado.motivo
    assert "8.69" in resultado.motivo


async def test_the_queued_rate_is_not_published() -> None:
    await run_seed()
    producto = await _producto()

    async with session_scope() as session:
        resultado = await revisar(
            session, _extraida("8.69"), producto=producto, vigente=None, referencia=None, url=URL
        )

    async with session_scope() as session:
        tasa = await session.get(Tasa, resultado.tasa_id)
        revision = await session.get(RevisionTasa, resultado.revision_id)

    assert tasa is not None and tasa.estado is EstadoTasa.PENDIENTE_REVISION
    assert tasa.fuente is FuenteTasa.FETCH_DIRIGIDO
    assert tasa.fuente_url == URL
    assert revision is not None and revision.estado is EstadoRevision.PENDIENTE
    assert revision.valor_nuevo == Decimal("8.69")
    assert revision.valor_anterior is None


# ─── Con una vigente previa ───────────────────────────────────


async def test_a_small_move_against_an_approved_rate_publishes_itself() -> None:
    """El caso frecuente del ciclo semanal: la tasa se movió décimas."""
    await run_seed()
    producto = await _producto()

    async with session_scope() as session:
        vigente = _tasa(
            producto.id, "8.69", fuente=FuenteTasa.FETCH_DIRIGIDO, estado=EstadoTasa.VIGENTE
        )
        session.add(vigente)
        await session.flush()
        resultado = await revisar(
            session,
            _extraida("8.90"),
            producto=producto,
            vigente=vigente,
            referencia=None,
            url=URL,
        )

    assert resultado.decision is Decision.PUBLICADA
    async with session_scope() as session:
        tasa = await session.get(Tasa, resultado.tasa_id)
    assert tasa is not None and tasa.estado is EstadoTasa.VIGENTE
    assert await _contar(RevisionTasa) == 0


async def test_a_move_beyond_tolerance_goes_to_review() -> None:
    await run_seed()
    producto = await _producto()

    async with session_scope() as session:
        vigente = _tasa(
            producto.id, "8.69", fuente=FuenteTasa.FETCH_DIRIGIDO, estado=EstadoTasa.VIGENTE
        )
        session.add(vigente)
        await session.flush()
        resultado = await revisar(
            session,
            _extraida("12.00"),
            producto=producto,
            vigente=vigente,
            referencia=None,
            url=URL,
        )

    assert resultado.decision is Decision.EN_REVISION
    assert "por encima de la tolerancia" in resultado.motivo
    async with session_scope() as session:
        revision = await session.get(RevisionTasa, resultado.revision_id)
    assert revision is not None
    assert revision.valor_anterior == Decimal("8.69")
    assert revision.valor_nuevo == Decimal("12.00")


async def test_low_confidence_goes_to_review_even_within_tolerance() -> None:
    """Si el extractor dudó, la decisión no es automática por poco que cambie."""
    await run_seed()
    producto = await _producto()

    async with session_scope() as session:
        vigente = _tasa(
            producto.id, "8.69", fuente=FuenteTasa.FETCH_DIRIGIDO, estado=EstadoTasa.VIGENTE
        )
        session.add(vigente)
        await session.flush()
        resultado = await revisar(
            session,
            _extraida("8.70", confianza="baja"),
            producto=producto,
            vigente=vigente,
            referencia=None,
            url=URL,
        )

    assert resultado.decision is Decision.EN_REVISION
    assert "confianza baja" in resultado.motivo


async def test_an_unchanged_rate_writes_nothing() -> None:
    """Un ciclo semanal que reescribe lo idéntico engorda la tabla sin informar."""
    await run_seed()
    producto = await _producto()

    async with session_scope() as session:
        vigente = _tasa(
            producto.id, "8.69", fuente=FuenteTasa.FETCH_DIRIGIDO, estado=EstadoTasa.VIGENTE
        )
        session.add(vigente)
        await session.flush()
        antes = await session.scalar(select(func.count()).select_from(Tasa))
        resultado = await revisar(
            session,
            _extraida("8.69"),
            producto=producto,
            vigente=vigente,
            referencia=None,
            url=URL,
        )
        despues = await session.scalar(select(func.count()).select_from(Tasa))

    assert resultado.decision is Decision.SIN_CAMBIO
    assert antes == despues
    assert resultado.tasa_id is None


# ─── Huecos de catálogo ───────────────────────────────────────


def test_a_catalogue_gap_is_not_a_review() -> None:
    """No hay `tasa_id` que darle a `revisiones_tasas` sin producto al que colgarla."""
    hueco = HuecoCatalogo(
        institucion="Finsus",
        producto="Plazo 360 días",
        plazo_dias=360,
        tasa_nominal=Decimal("8.69"),
        url=URL,
    )

    serializado = hueco.como_dict()

    assert serializado["plazo_dias"] == 360
    assert serializado["tasa_nominal"] == "8.69"
    assert serializado["url"] == URL


def test_the_report_counts_each_decision() -> None:
    reporte = ReporteRevision()
    reporte.registrar(Resultado(Decision.PUBLICADA, ""))
    reporte.registrar(Resultado(Decision.EN_REVISION, ""))
    reporte.registrar(Resultado(Decision.EN_REVISION, ""))
    reporte.registrar(Resultado(Decision.SIN_CAMBIO, ""))

    assert (reporte.publicadas, reporte.en_revision, reporte.sin_cambio) == (1, 2, 1)


async def test_a_second_run_the_same_day_is_idempotent() -> None:
    """Un reintento del job un lunes que falló a la mitad no puede reventar.

    `tasas` tiene clave única `(producto, fecha, fuente)`: sin esta
    comprobación, la segunda corrida choca en cada producto ya encolado.
    """
    await run_seed()
    producto = await _producto()

    async with session_scope() as session:
        primera = await revisar(
            session, _extraida("8.69"), producto=producto, vigente=None, referencia=None, url=URL
        )

    async with session_scope() as session:
        segunda = await revisar(
            session, _extraida("8.75"), producto=producto, vigente=None, referencia=None, url=URL
        )

    assert primera.decision is Decision.EN_REVISION
    assert segunda.decision is Decision.SIN_CAMBIO
    assert "ya hay una lectura" in segunda.motivo
    # Una sola observación y una sola revisión: nada duplicado.
    assert await _contar(Tasa) == 1
    assert await _contar(RevisionTasa) == 1


# ─── Escaleras por saldo ──────────────────────────────────────


async def test_the_first_ladder_reading_goes_to_review_with_the_ladder_visible() -> None:
    """La escalera entera es UNA observación, y quien revisa la ve en el motivo."""
    await run_seed()
    producto = await _producto()
    escalera = _escalera_openbank()

    async with session_scope() as session:
        resultado = await revisar(
            session,
            escalera.cabeza,
            producto=producto,
            vigente=None,
            referencia=None,
            url=URL,
            escalera=escalera,
        )

    assert resultado.decision is Decision.EN_REVISION
    assert "$30,000" in resultado.motivo
    assert "6.30%" in resultado.motivo

    async with session_scope() as session:
        tasa = await session.scalar(select(Tasa).where(Tasa.id == resultado.tasa_id))
        assert tasa is not None
        assert tasa.tasa_nominal == Decimal("13.00")  # la titular ES el tramo 1
        assert [(t.desde, t.hasta) for t in tasa.tramos] == [
            (Decimal("0.00"), Decimal("30000.00")),
            (Decimal("30000.00"), None),
        ]
        revision = await session.get(RevisionTasa, resultado.revision_id)
        assert revision is not None
        assert revision.valor_nuevo == Decimal("13.00")


async def test_an_identical_ladder_is_sin_cambio() -> None:
    await run_seed()
    producto = await _producto()

    async with session_scope() as session:
        vigente = _tasa(
            producto.id,
            "13.00",
            fuente=FuenteTasa.FETCH_DIRIGIDO,
            estado=EstadoTasa.VIGENTE,
            tramos=_tramos_orm(),
        )
        session.add(vigente)
        await session.flush()
        antes = await session.scalar(select(func.count()).select_from(Tasa))
        resultado = await revisar(
            session,
            _escalera_openbank().cabeza,
            producto=producto,
            vigente=vigente,
            referencia=None,
            url=URL,
            escalera=_escalera_openbank(),
        )
        despues = await session.scalar(select(func.count()).select_from(Tasa))

    assert resultado.decision is Decision.SIN_CAMBIO
    assert antes == despues


async def test_a_flat_vigente_against_a_ladder_goes_to_review() -> None:
    """Mismo titular, estructura nueva: NO es «sin cambio», es el dato a revisar."""
    await run_seed()
    producto = await _producto()
    escalera = _escalera_openbank()

    async with session_scope() as session:
        vigente = _tasa(
            producto.id, "13.00", fuente=FuenteTasa.FETCH_DIRIGIDO, estado=EstadoTasa.VIGENTE
        )
        session.add(vigente)
        await session.flush()
        resultado = await revisar(
            session,
            escalera.cabeza,
            producto=producto,
            vigente=vigente,
            referencia=None,
            url=URL,
            escalera=escalera,
        )

    assert resultado.decision is Decision.EN_REVISION
    assert "cambió de estructura" in resultado.motivo
    assert "plana" in resultado.motivo


async def test_a_tier_move_within_tolerance_publishes_the_new_ladder() -> None:
    """El caso frecuente escalonado: un tramo se movió décimas."""
    await run_seed()
    producto = await _producto()

    async with session_scope() as session:
        vigente = _tasa(
            producto.id,
            "13.00",
            fuente=FuenteTasa.FETCH_DIRIGIDO,
            estado=EstadoTasa.VIGENTE,
            tramos=_tramos_orm(baja="6.30"),
        )
        session.add(vigente)
        await session.flush()
        resultado = await revisar(
            session,
            _escalera_openbank(baja="6.40").cabeza,
            producto=producto,
            vigente=vigente,
            referencia=None,
            url=URL,
            escalera=_escalera_openbank(baja="6.40"),
        )

    assert resultado.decision is Decision.PUBLICADA
    async with session_scope() as session:
        tasa = await session.get(Tasa, resultado.tasa_id)
        assert tasa is not None and tasa.estado is EstadoTasa.VIGENTE
        assert tasa.tramos[1].tasa_nominal == Decimal("6.40")


async def test_a_big_tier_move_goes_to_review_even_with_the_same_headline() -> None:
    """El titular no se movió; el tramo bajo saltó 2.7 pp. La tolerancia mira
    la escalera completa, no solo la cabecera."""
    await run_seed()
    producto = await _producto()

    async with session_scope() as session:
        vigente = _tasa(
            producto.id,
            "13.00",
            fuente=FuenteTasa.FETCH_DIRIGIDO,
            estado=EstadoTasa.VIGENTE,
            tramos=_tramos_orm(baja="6.30"),
        )
        session.add(vigente)
        await session.flush()
        resultado = await revisar(
            session,
            _escalera_openbank(baja="9.00").cabeza,
            producto=producto,
            vigente=vigente,
            referencia=None,
            url=URL,
            escalera=_escalera_openbank(baja="9.00"),
        )

    assert resultado.decision is Decision.EN_REVISION
    assert "tramo se movió 2.70 pp" in resultado.motivo
