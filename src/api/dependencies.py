"""Dependencias compartidas de la API: auth, sesión y contexto de cálculo.

**Auth de dos niveles.** La API no se expone a internet, pero eso no la
convierte en pública: el BFF de Astro sólo necesita leer, y el admin escribe.
Dos llaves distintas hacen que una filtración de la del BFF —que vive en un
contenedor que sirve tráfico público— no permita alterar datos.

La comparación es en tiempo constante (`secrets.compare_digest`). Comparar con
`==` filtra por temporización cuántos caracteres coinciden, y en una llave que
protege escrituras eso importa.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config_store import effective
from core.db import get_sessionmaker
from core.logging import get_logger
from core.settings import settings
from domain.models import ParametrosFiscales, UmbralesBanderas
from domain.orm import ParametroFiscal, SerieEconomica, ValorSerieEconomica

log = get_logger(__name__)

CLAVE_SERIE_UDI = "SP68257"
CLAVE_SERIE_INPC = "SP1"


async def get_session() -> AsyncIterator[AsyncSession]:
    """Sesión de sólo lectura por petición. Los routers que escriben hacen commit."""
    async with get_sessionmaker()() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


# ─── Autenticación ────────────────────────────────────────────


def _valida(recibida: str | None, esperada: str) -> bool:
    if not recibida or not esperada:
        return False
    return secrets.compare_digest(recibida, esperada)


def _no_autorizado() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Se requiere una X-API-Key válida",
        headers={"WWW-Authenticate": "X-API-Key"},
    )


async def requiere_lectura(
    x_api_key: Annotated[str | None, Header()] = None,
) -> str:
    """Acepta la llave de lectura **o** la de admin.

    El admin puede leer todo lo que lee el BFF; lo contrario no.
    """
    lectura = settings.api_read_key.get_secret_value()
    admin = settings.api_admin_key.get_secret_value()

    if _valida(x_api_key, admin):
        return "admin"
    if _valida(x_api_key, lectura):
        return "lectura"
    raise _no_autorizado()


async def requiere_admin(
    x_api_key: Annotated[str | None, Header()] = None,
) -> str:
    """Sólo la llave de admin. La de lectura recibe 403, no 401.

    La distinción importa: 401 significa "identifícate", 403 significa "te
    identificaste pero esto no te toca". Devolver 401 a una llave válida
    llevaría al cliente a reintentar con la misma credencial.
    """
    admin = settings.api_admin_key.get_secret_value()
    if _valida(x_api_key, admin):
        return "admin"

    if _valida(x_api_key, settings.api_read_key.get_secret_value()):
        log.warning("admin_denegado_con_llave_de_lectura")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Esta operación requiere la llave de administración",
        )
    raise _no_autorizado()


LecturaDep = Annotated[str, Depends(requiere_lectura)]
AdminDep = Annotated[str, Depends(requiere_admin)]


# ─── Contexto de cálculo ──────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ContextoMercado:
    """Todo lo que el motor de métricas necesita y no está en el producto.

    Se resuelve una vez por petición y se pasa a todos los cálculos, para que
    todas las filas de un comparador se calculen con la misma UDI y la misma
    inflación. Resolverlo por fila abriría la puerta a que dos productos se
    comparen con contextos distintos.
    """

    valor_udi: Decimal
    inflacion_anual: Decimal
    params_fiscales: ParametrosFiscales
    umbrales: UmbralesBanderas
    #: Si está activo, el catálogo incluye también las tasas en
    #: PENDIENTE_REVISION, siempre marcadas «sin verificar». Viaja en el
    #: contexto y no se lee del ConfigStore en cada servicio para que todas
    #: las filas de una respuesta se resuelvan con el mismo criterio, igual
    #: que la UDI y la inflación.
    incluir_sin_verificar: bool = False


async def _ultimo_valor_serie(session: AsyncSession, clave: str) -> Decimal | None:
    """El último valor **vigente hoy** de una serie.

    El filtro por fecha no es una precaución teórica: **Banxico publica varias
    series por adelantado.** Medido el 2026-07-31 contra el SIE, con la
    sincronización de la fase 7 recién corrida: la UDI llegaba hasta el 10 de
    agosto, el tipo de cambio FIX hasta el 4, la TIIE hasta el 3 y la tasa
    objetivo hasta el 1.

    Sin el filtro, el «último valor» de la UDI es el de dentro de diez días y
    los límites de cobertura IPAB y PROSOFIPO en pesos se calculan con uno que
    todavía no rige — en esa medición, 8.797743 en vez de 8.793839. Mientras la
    tabla se llenaba desde el seed no pasaba, porque el CSV no trae fechas
    futuras: el fallo entra con la ingesta, no con el código que la consume.
    """
    valor: Decimal | None = await session.scalar(
        select(ValorSerieEconomica.valor)
        .join(SerieEconomica)
        .where(
            SerieEconomica.clave_banxico == clave,
            ValorSerieEconomica.fecha <= date.today(),
        )
        .order_by(desc(ValorSerieEconomica.fecha))
        .limit(1)
    )
    return valor


async def _inflacion_anual(session: AsyncSession) -> Decimal | None:
    """Variación del INPC contra el mismo mes del año anterior.

    Se calcula aquí y no se almacena porque es una derivada de la serie: si se
    guardara, habría dos fuentes de verdad que podrían discrepar.
    """
    filas = (
        (
            await session.execute(
                select(ValorSerieEconomica.fecha, ValorSerieEconomica.valor)
                .join(SerieEconomica)
                .where(
                    SerieEconomica.clave_banxico == CLAVE_SERIE_INPC,
                    # El INPC se publica con rezago y nunca por adelantado, así
                    # que este filtro hoy no descarta nada. Va igualmente para
                    # que las dos series se resuelvan con la misma regla: «lo
                    # último que ya rige», no «lo último que hay».
                    ValorSerieEconomica.fecha <= date.today(),
                )
                .order_by(desc(ValorSerieEconomica.fecha))
                .limit(13)
            )
        )
        .tuples()
        .all()
    )
    if len(filas) < 13:
        return None

    actual, hace_un_anio = filas[0][1], filas[12][1]
    if not hace_un_anio:
        return None
    return ((actual / hace_un_anio) - 1) * 100


async def _params_fiscales(session: AsyncSession) -> ParametrosFiscales:
    """Los del ejercicio en curso, o los más recientes que haya."""
    fila = await session.scalar(
        select(ParametroFiscal)
        .where(ParametroFiscal.anio <= date.today().year)
        .order_by(desc(ParametroFiscal.anio))
        .limit(1)
    )
    if fila is None:
        # Sin parámetros no se puede calcular ISR, y calcular sin ISR daría
        # números optimistas: mejor fallar visible.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No hay parámetros fiscales cargados. Ejecuta `python -m cli seed`.",
        )
    return ParametrosFiscales.model_validate(fila)


def umbrales_desde_config() -> UmbralesBanderas:
    """Arma los umbrales desde ConfigStore.

    Éste es el único punto donde `metrics.flags` se conecta con la
    configuración: el motor recibe el objeto ya construido y sigue siendo puro.
    """
    return UmbralesBanderas(
        imor_amarilla=effective.umbral_imor_amarilla,
        imor_roja=effective.umbral_imor_roja,
        icap_amarilla=effective.umbral_icap_amarilla,
        icap_roja=effective.umbral_icap_roja,
        cobertura_amarilla=effective.umbral_cobertura_amarilla,
        cobertura_roja=effective.umbral_cobertura_roja,
        gat_inconsistencia_pp=effective.umbral_gat_inconsistencia_pp,
        crecimiento_captacion_pct=effective.umbral_crecimiento_captacion_pct,
        tasa_sobre_mercado_pp=effective.umbral_tasa_sobre_mercado_pp,
        apalancamiento_amarilla=effective.umbral_apalancamiento_amarilla,
    )


async def get_contexto(session: SessionDep) -> ContextoMercado:
    """Resuelve el contexto de mercado de la petición.

    UDI e inflación salen de las series de Banxico; si la tabla está vacía
    —porque la ingesta de la fase 7 aún no existe o falló— se cae a los valores
    de respaldo del ConfigStore en lugar de dejar de servir.
    """
    udi = await _ultimo_valor_serie(session, CLAVE_SERIE_UDI)
    if udi is None:
        udi = effective.udi_valor_fallback
        log.warning("udi_desde_fallback", valor=str(udi))

    inflacion = await _inflacion_anual(session)
    if inflacion is None:
        inflacion = effective.inflacion_anual_fallback
        log.warning("inflacion_desde_fallback", valor=str(inflacion))

    return ContextoMercado(
        valor_udi=udi,
        inflacion_anual=inflacion,
        params_fiscales=await _params_fiscales(session),
        umbrales=umbrales_desde_config(),
        incluir_sin_verificar=effective.mostrar_tasas_sin_verificar,
    )


ContextoDep = Annotated[ContextoMercado, Depends(get_contexto)]


__all__ = [
    "CLAVE_SERIE_INPC",
    "CLAVE_SERIE_UDI",
    "AdminDep",
    "ContextoDep",
    "ContextoMercado",
    "LecturaDep",
    "SessionDep",
    "get_contexto",
    "get_session",
    "requiere_admin",
    "requiere_lectura",
    "umbrales_desde_config",
]
