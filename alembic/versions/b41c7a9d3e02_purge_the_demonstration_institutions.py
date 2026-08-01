"""purge the demonstration institutions

Las dos instituciones ficticias (Ahorra+ Capital, Alcancía Fuerte) salen del
producto: el sitio pasa a llenarse con datos reales y el catálogo semilla de
este mismo commit ya no las trae — pero el seed sólo hace upsert, nunca borra,
así que sin esta migración seguirían sirviéndose desde la base de producción
para siempre. `tasas` es append-only **para observaciones reales**; estas
filas nunca observaron nada, así que borrarlas no rompe la doctrina, la aplica.

También retira del ConfigStore la entrada `mostrar_datos_demo`: la llave
desaparece del registro (la sustituye `mostrar_tasas_sin_verificar`) y una
entrada huérfana provocaría el aviso de llave desconocida en cada snapshot.
El historial de `config_versions` se conserva: es la bitácora.

Idempotente: re-ejecutar no encuentra filas. En el deploy corre antes del
seed (`desplegar.sh` migra y luego siembra), que es el orden que la hace
segura.

Revision ID: b41c7a9d3e02
Revises: 6c5b2ed4d85f
Create Date: 2026-07-31 18:05:12.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b41c7a9d3e02"
down_revision: str | None = "6c5b2ed4d85f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEMO = "SELECT id FROM instituciones WHERE es_demostracion"
_PRODUCTOS_DEMO = f"SELECT id FROM productos WHERE institucion_id IN ({_DEMO})"


def upgrade() -> None:
    # Orden FK explícito aunque los FK lleven ON DELETE CASCADE: una migración
    # dice lo que hace, no lo delega en la configuración del esquema vigente.
    op.execute(
        f"DELETE FROM revisiones_tasas WHERE tasa_id IN (SELECT id FROM tasas WHERE producto_id IN ({_PRODUCTOS_DEMO}))"
    )
    op.execute(f"DELETE FROM tasas WHERE producto_id IN ({_PRODUCTOS_DEMO})")
    op.execute(f"DELETE FROM indicadores_financieros WHERE institucion_id IN ({_DEMO})")
    op.execute(f"DELETE FROM banderas WHERE institucion_id IN ({_DEMO})")
    op.execute(f"DELETE FROM fuentes_tasas WHERE institucion_id IN ({_DEMO})")
    op.execute(f"DELETE FROM productos WHERE institucion_id IN ({_DEMO})")
    op.execute("DELETE FROM instituciones WHERE es_demostracion")
    op.execute("DELETE FROM config_store WHERE key = 'mostrar_datos_demo'")


def downgrade() -> None:
    # No-op consciente: las filas eran ficticias, ya no existen en las
    # semillas y ningún rollback de código las necesita para servir. Fallar
    # dejaría el `alembic downgrade -1` del runbook inservible justo el día
    # que hace falta — el mismo razonamiento que 6c5b2ed4d85f.
    pass
