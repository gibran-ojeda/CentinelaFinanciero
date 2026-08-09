"""blank the unverified aggregator notes

Las observaciones de agregador de julio de 2026 llevan en `notas` la bitácora
del analista («Sin verificar contra la pagina oficial…», «candidata a bandera
compuesta RED_FLAG_TASA…», «devolvio 403…»), y ese campo se publica como
`condiciones` en los chips del comparador y de la ficha: texto de trabajo
interno servido al usuario final.

Las semillas se limpiaron en 95cb800, pero el importador salta las filas ya
existentes (mismo producto, fecha y fuente cuentan como duplicado) y `tasas`
es append-only, así que la única vía a esas filas es una migración de datos.
Append-only protege **observaciones**: la tasa, su fecha y su fuente quedan
intactas; estas `notas` nunca observaron una condición del producto — eran
contabilidad de investigación publicada por accidente. Vaciarlas no rompe la
doctrina, la aplica. El estado de verificación tampoco se pierde: viaja en
`estado` y la UI ya lo cuenta (fecha en ámbar y pastilla «sin verificar»).

Idempotente: el prefijo «Sin verificar» cubre las dos variantes observadas,
ninguna nota legítima de las semillas empieza así, y re-ejecutar —o correr
sobre una base recién sembrada— no encuentra filas.

Revision ID: 8e6b2d94f7a3
Revises: f0a2c9d47b31
Create Date: 2026-08-09 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "8e6b2d94f7a3"
down_revision: str | None = "f0a2c9d47b31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE tasas SET notas = NULL WHERE notas LIKE 'Sin verificar%'")


def downgrade() -> None:
    # No-op consciente: restaurar el texto re-publicaría bitácora interna como
    # condiciones, que es exactamente el daño que esta migración corrige, y
    # fallar dejaría el `alembic downgrade -1` del runbook inservible — el
    # mismo razonamiento que b41c7a9d3e02.
    pass
