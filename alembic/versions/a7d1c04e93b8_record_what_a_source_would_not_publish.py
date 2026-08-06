"""record what a source would not publish

Revision ID: a7d1c04e93b8
Revises: e2f7b3c85a41
Create Date: 2026-08-06 18:20:00.000000

Mercado Pago publica «Ganancias de hasta 12 % anual» en `/cuenta` y nada más:
ni plazo, ni monto, ni tope, ni condición. La página se descarga perfectamente,
el extractor obedece la regla 1 y no publica nada, y el resultado es
indistinguible de una portada que no habla de tasas — las dos vuelven con
`tasas: []`, las dos dejan `ultimo_exito_at` en NULL. Para quien compara no son
lo mismo en absoluto: una institución no tiene página de tasas y la otra
anuncia un número que nadie puede saber si le tocará.

Estas dos columnas guardan lo que el extractor descartó, y de ahí sale la
bandera de tasas ambiguas. Cada lectura las sobrescribe: describen lo que la
página dice hoy, no su historia.

Sin backfill, por el mismo motivo que la migración de salud: el histórico no
existe y fabricarlo sería inventarlo. La primera corrida deja el estado real.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7d1c04e93b8"
down_revision: str | None = "e2f7b3c85a41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("fuentes_tasas", sa.Column("ultima_ambiguedad", sa.Text(), nullable=True))
    op.add_column(
        "fuentes_tasas",
        sa.Column("ultima_ambiguedad_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("fuentes_tasas", "ultima_ambiguedad_at")
    op.drop_column("fuentes_tasas", "ultima_ambiguedad")
