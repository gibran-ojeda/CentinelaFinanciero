"""Cobertura de seguro de depósitos (§4.6 del foundation).

Los límites de protección están fijados **en UDIs**, no en pesos: el IPAB cubre
400,000 UDIs y PROSOFIPO 25,000. Guardarlos en pesos sería congelar un número
que cambia todos los días con el valor de la UDI, y acabaría mostrando una
cobertura equivocada al usuario. Se convierten al vuelo con la serie de Banxico
(§16, nota de diseño).

Este dato se muestra siempre junto a la tasa, nunca en letra chica: es la
diferencia entre perder el dinero y recuperarlo si la institución quiebra.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from domain.enums import TipoSeguro
from metrics.rounding import CENTAVO, redondear

#: Límites en UDIs por fondo de protección. Constantes de dominio: los fija la
#: ley, no la configuración.
LIMITE_IPAB_UDIS = Decimal("400000")
LIMITE_PROSOFIPO_UDIS = Decimal("25000")

LIMITES_UDIS: dict[TipoSeguro, Decimal | None] = {
    # Deuda soberana: el obligado es el Gobierno Federal, no hay fondo ni tope.
    TipoSeguro.SOBERANO: None,
    TipoSeguro.IPAB: LIMITE_IPAB_UDIS,
    TipoSeguro.PROSOFIPO: LIMITE_PROSOFIPO_UDIS,
    TipoSeguro.NINGUNO: Decimal("0"),
}


@dataclass(frozen=True, slots=True)
class Cobertura:
    """Cobertura resuelta, lista para mostrar."""

    tipo: TipoSeguro
    limite_udis: Decimal | None
    limite_mxn: Decimal | None
    """`None` significa **sin límite**, no "desconocido"."""

    valor_udi: Decimal

    @property
    def sin_limite(self) -> bool:
        return self.limite_mxn is None

    @property
    def sin_cobertura(self) -> bool:
        return self.limite_mxn == 0

    def cubre(self, monto: Decimal) -> bool:
        """¿El monto queda íntegramente protegido?"""
        if self.sin_limite:
            return True
        return self.limite_mxn is not None and monto <= self.limite_mxn

    def monto_expuesto(self, monto: Decimal) -> Decimal:
        """Parte del monto que quedaría sin protección."""
        if self.sin_limite:
            return Decimal("0.00")
        limite = self.limite_mxn or Decimal("0")
        return redondear(max(monto - limite, Decimal("0")), CENTAVO)


def cobertura_mxn(tipo_seguro: TipoSeguro, valor_udi: Decimal) -> Decimal | None:
    """Límite de cobertura en pesos. `None` = sin límite (deuda soberana)."""
    if valor_udi <= 0:
        raise ValueError("el valor de la UDI debe ser positivo")
    limite_udis = LIMITES_UDIS[tipo_seguro]
    if limite_udis is None:
        return None
    return redondear(limite_udis * valor_udi, CENTAVO)


def resolver_cobertura(tipo_seguro: TipoSeguro, valor_udi: Decimal) -> Cobertura:
    return Cobertura(
        tipo=tipo_seguro,
        limite_udis=LIMITES_UDIS[tipo_seguro],
        limite_mxn=cobertura_mxn(tipo_seguro, valor_udi),
        valor_udi=valor_udi,
    )


__all__ = [
    "LIMITES_UDIS",
    "LIMITE_IPAB_UDIS",
    "LIMITE_PROSOFIPO_UDIS",
    "Cobertura",
    "cobertura_mxn",
    "resolver_cobertura",
]
