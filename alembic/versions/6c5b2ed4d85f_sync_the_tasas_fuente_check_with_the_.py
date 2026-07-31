"""sync the tasas fuente check with the enum

La fase 9 añadió `FuenteTasa.AGREGADOR` para que la procedencia dejara de
mentir, pero el CHECK de la base se quedó con los cinco valores que escribió la
migración inicial. El ORM no lo notó porque deriva la restricción del enum
—`_enum_check()` en orm.py—, así que `Base.metadata` estaba bien y la base mal.

En desarrollo no se vio porque la base ya tenía las filas cargadas de antes,
con la etiqueta vieja. Se vio en el primer despliegue sobre una base limpia:
`cli tasas import` reventó con `CheckViolationError` al insertar las 30 filas
de contraste, y se llevó por delante el despliegue entero.

Revision ID: 6c5b2ed4d85f
Revises: 51e0c5001154
Create Date: 2026-07-31 16:31:37.472721
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "6c5b2ed4d85f"
down_revision: str | None = "51e0c5001154"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Literales y no derivados de `FuenteTasa` a propósito. Una migración es la
#: foto de un momento: si leyera el enum, su significado cambiaría cada vez que
#: alguien añada un valor, y dejaría de reproducir el esquema que dice producir.
_VALORES = "'MANUAL', 'AGREGADOR', 'BANXICO_API', 'CNBV', 'FETCH_DIRIGIDO', 'LLM_RESEARCH'"
_VALORES_PREVIOS = "'MANUAL', 'BANXICO_API', 'CNBV', 'FETCH_DIRIGIDO', 'LLM_RESEARCH'"


def upgrade() -> None:
    op.drop_constraint("ck_fuente_valido", "tasas", type_="check")
    op.create_check_constraint("ck_fuente_valido", "tasas", f"fuente IN ({_VALORES})")


def downgrade() -> None:
    # Estrechar el CHECK exige que no quede ninguna fila con el valor que se
    # retira, o Postgres se niega a crearlo. Se borran, y es una decisión
    # consciente: las filas AGREGADOR son datos de contraste que se regeneran
    # con `cli tasas import seeds/tasas.csv`, y nunca son publicables —la
    # invariante «AGREGADOR jamás VIGENTE» las mantiene fuera del sitio—, así
    # que no hay nada que un rollback pueda perder de forma irreversible.
    # La alternativa, fallar, dejaría el `alembic downgrade -1` del runbook
    # inservible justo el día que hace falta.
    op.execute("DELETE FROM tasas WHERE fuente = 'AGREGADOR'")
    op.drop_constraint("ck_fuente_valido", "tasas", type_="check")
    op.create_check_constraint("ck_fuente_valido", "tasas", f"fuente IN ({_VALORES_PREVIOS})")
