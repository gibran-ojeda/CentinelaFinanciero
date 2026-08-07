"""sync the bandera tipo check with the enum

`TipoBandera.TASAS_AMBIGUAS` entró en el ORM, que deriva su CHECK del enum con
`_enum_check()`, así que `Base.metadata` quedó bien y la base mal — el CHECK de
`banderas` seguía con los nueve valores de la migración inicial.

Es exactamente lo que pasó con `FuenteTasa.AGREGADOR` en `6c5b2ed4d85f`, y por
la misma razón: los tests montan el esquema con `create_all`, así que la
restricción que prueban es la del ORM y nadie mira la de la base. Aquella vez
se descubrió en el primer despliegue sobre una base limpia, con el despliegue
ya a medias. Ésta se descubrió corriendo el job contra la base local, y esta
vez el test que faltaba va con ella (`test_migrations.py`).

Sin backfill ni riesgo de datos: sólo se amplía lo que se acepta.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d3b9a6e15c74"
down_revision: str | None = "a7d1c04e93b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Literales y no derivados de `TipoBandera` a propósito, como en
#: `6c5b2ed4d85f`: una migración es la foto de un momento. Si leyera el enum,
#: su significado cambiaría cada vez que alguien añada un valor y dejaría de
#: reproducir el esquema que dice producir.
_PREVIOS = (
    "'IMOR', 'COBERTURA_CARTERA', 'ICAP', 'NICAP', 'APALANCAMIENTO', "
    "'NO_RECOMENDABLE', 'RED_FLAG_TASA', 'GAT_INCONSISTENTE', 'SIN_COBERTURA'"
)
_VALORES = f"{_PREVIOS}, 'TASAS_AMBIGUAS'"


def upgrade() -> None:
    op.drop_constraint("ck_tipo_valido", "banderas", type_="check")
    op.create_check_constraint("ck_tipo_valido", "banderas", f"tipo IN ({_VALORES})")


def downgrade() -> None:
    # Estrechar el CHECK exige que no quede ninguna fila con el valor que se
    # retira. Se borran, y no se pierde nada irreversible: las banderas son
    # derivadas —`recomputar()` las reconstruye enteras en cada corrida— y
    # ésta en concreto se vuelve a emitir en cuanto una lectura vuelva a
    # encontrar un anuncio sin concretar. Fallar aquí dejaría el
    # `alembic downgrade -1` del runbook inservible justo el día que hace falta.
    op.execute("DELETE FROM banderas WHERE tipo = 'TASAS_AMBIGUAS'")
    op.drop_constraint("ck_tipo_valido", "banderas", type_="check")
    op.create_check_constraint("ck_tipo_valido", "banderas", f"tipo IN ({_PREVIOS})")
