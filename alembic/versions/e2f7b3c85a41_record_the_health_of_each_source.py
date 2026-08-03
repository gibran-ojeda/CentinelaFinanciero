"""record the health of each source

Revision ID: e2f7b3c85a41
Revises: c9d4e7a1f826
Create Date: 2026-08-02 23:40:00.000000

El 2026-08-02 cinco de las catorce fuentes de nivel 2 estaban rotas —dos
dominios muertos, un 403 permanente, un host sin DNS y una página que responde
200 sin texto legible— y la corrida se marcó EXITOSO. `activa` sólo la escribía
el seed, `ultima_extraccion_at` no la leía nadie, el circuito del fetcher muere
con el proceso y la única alarma exigía que fallaran las dieciocho a la vez.
Entre corrida y corrida no quedaba ni un rastro.

`ultimo_exito_at` es la columna que distingue lo que `ultima_extraccion_at` no
podía: seis fuentes más apuntan a portadas que se descargan perfectamente y no
publican ninguna tasa. Se leen, no cuentan como error, y no sirven para nada.

Sin backfill: `fallos_consecutivos` arranca en cero y las dos fechas en NULL
para todas. Es lo correcto — el histórico no existe y fabricarlo sería
inventarlo. La primera corrida tras la migración deja el estado real.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2f7b3c85a41"
down_revision: str | None = "c9d4e7a1f826"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `server_default="0"` y no sólo el default del ORM: sin él las filas que
    # ya existen quedarían con NULL en una columna NOT NULL y el ALTER falla.
    op.add_column(
        "fuentes_tasas",
        sa.Column("fallos_consecutivos", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("fuentes_tasas", sa.Column("ultimo_error", sa.Text(), nullable=True))
    op.add_column(
        "fuentes_tasas",
        sa.Column("ultimo_exito_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("fuentes_tasas", sa.Column("pausada_motivo", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_fallos_no_negativos", "fuentes_tasas", "fallos_consecutivos >= 0"
    )


def downgrade() -> None:
    op.drop_constraint("ck_fallos_no_negativos", "fuentes_tasas", type_="check")
    op.drop_column("fuentes_tasas", "pausada_motivo")
    op.drop_column("fuentes_tasas", "ultimo_exito_at")
    op.drop_column("fuentes_tasas", "ultimo_error")
    op.drop_column("fuentes_tasas", "fallos_consecutivos")
