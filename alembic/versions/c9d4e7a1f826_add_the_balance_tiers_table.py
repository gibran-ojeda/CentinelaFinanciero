"""add the balance tiers table

Revision ID: c9d4e7a1f826
Revises: b41c7a9d3e02
Create Date: 2026-08-01 10:20:00.000000

Openbank paga 13% por los primeros $30,000 y 6.3% de ahí a $1,000,000; una
fila de `tasas` no puede decir eso sin mentir en uno de los dos tramos. La
escalera se guarda como tabla hija de la **observación** —no del producto—
para que `tasas` siga siendo append-only y la historia de escaleras quede
auditable: cada lectura trae su snapshot completo de tramos.

Cero filas hijas = tasa plana, que es todo el catálogo existente; por eso no
hay backfill ni `server_default`. El no-solape y la contigüidad no caben en un
CHECK portable (EXCLUDE USING gist es solo-Postgres y los tests corren también
sobre SQLite): los hace cumplir `metrics.tramos.validar_escalera`, el único
constructor por el que pasan todos los escritores.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9d4e7a1f826"
down_revision: str | None = "b41c7a9d3e02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tramos_tasas",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tasa_id", sa.Integer(), nullable=False),
        sa.Column("desde", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("hasta", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("tasa_nominal", sa.Numeric(precision=8, scale=4), nullable=False),
        sa.CheckConstraint("desde >= 0", name="ck_tramo_desde_no_negativo"),
        sa.CheckConstraint(
            "hasta IS NULL OR hasta > desde", name="ck_tramo_hasta_mayor_que_desde"
        ),
        sa.CheckConstraint("tasa_nominal >= 0", name="ck_tramo_tasa_no_negativa"),
        sa.ForeignKeyConstraint(["tasa_id"], ["tasas.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tasa_id", "desde", name="uq_tramo_desde"),
    )


def downgrade() -> None:
    op.drop_table("tramos_tasas")
