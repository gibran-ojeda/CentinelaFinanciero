"""add demonstration flag to instituciones

Revision ID: 51e0c5001154
Revises: 4dc69f00aae0
Create Date: 2026-07-25 16:47:29.127662
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "51e0c5001154"
down_revision: str | None = "4dc69f00aae0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `server_default` no viene del autogenerate y es imprescindible: la tabla
    # ya tiene instituciones cargadas, y una columna NOT NULL sin default falla
    # sobre cualquier fila existente. Se deja puesto en vez de retirarlo tras
    # el backfill para que un INSERT que omita la columna siga siendo válido:
    # el default correcto —y seguro— de una institución es "no es demo".
    op.add_column(
        "instituciones",
        sa.Column(
            "es_demostracion",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("instituciones", "es_demostracion")
