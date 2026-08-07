"""record the name an institution publishes

Revision ID: f0a2c9d47b31
Revises: d3b9a6e15c74
Create Date: 2026-08-07 10:15:00.000000

Klar publica «Cuenta» al 3 % e «Inversión Flexible» al 6 %, las dos a la vista
y sin plazo. El pipeline indexa el catálogo por `(tipo, plazo)`, así que las
dos caen en la misma casilla y nada puede decidir cuál de ellas es el producto
que tenemos dado de alta: desde `85e67c7` las dos salen como hueco, que es el
fallo seguro pero deja esas tasas sin publicar para siempre.

`nombre_publicado` es la clave de mapeo que faltaba, con el mismo papel que
`Institucion.nombre_cnbv` cumple desde la fase 8. Anulable a propósito: sólo lo
necesitan las casillas ambiguas, y donde el plazo ya distingue no se pone.

Sin backfill: ningún producto tiene hoy homónimo en su casilla, así que dejarla
en NULL para todos reproduce exactamente el comportamiento actual.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f0a2c9d47b31"
down_revision: str | None = "d3b9a6e15c74"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("productos", sa.Column("nombre_publicado", sa.String(160), nullable=True))
    op.create_index("ix_productos_nombre_publicado", "productos", ["nombre_publicado"])


def downgrade() -> None:
    op.drop_index("ix_productos_nombre_publicado", table_name="productos")
    op.drop_column("productos", "nombre_publicado")
