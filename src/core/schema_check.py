"""Comprobación de deriva de esquema. Gate duro del despliegue.

Corre como `python -m core.schema_check` dentro del contenedor, justo después
de `alembic upgrade head`, y aborta el deploy si la base real no coincide con
lo que el código espera.

El contrato se **deriva del `Base.metadata` del ORM**, no de una lista escrita
a mano: una lista a mano se olvida de actualizar y entonces el gate deja de
comprobar justo lo que se acaba de añadir. Aquí, si alguien agrega una columna
al modelo y no genera la migración, el gate lo ve solo.

Dos preguntas, y las dos importan:

1. ¿Está la base en el head de las migraciones del disco? Una migración sin
   aplicar es la causa más común de un 500 a los cinco minutos del deploy.
2. ¿Coincide el esquema real con el del ORM? Puede estar en el head y aun así
   diferir: alguien tocó la base a mano, o una migración se escribió a medias.

3. ¿Aceptan las restricciones de enumeración los valores que el código usa? El
   ORM las deriva del enum (`_enum_check()` en orm.py) y la migración las
   escribió literales, así que añadir un miembro al enum sin migración deja la
   base rechazando un valor que el código da por válido. Pasó de verdad con
   `FuenteTasa.AGREGADOR`: el despliegue murió al sembrar, en la base limpia
   donde nadie lo había probado.

Lo que **no** comprueba: tipos exactos por dialecto ni índices. Comparar
`VARCHAR(64)` contra `String(64)` a través de todos los dialectos es una fuente
de falsos positivos que acabaría con alguien desactivando el gate — que es el
peor resultado posible. Tablas, columnas, nulabilidad y enumeraciones cubren lo
que rompe en producción.

Las enumeraciones sí se comparan, y no contradice lo anterior: los dos lados
son conjuntos de literales, no tipos de un dialecto, así que la comparación es
exacta y no puede producir un falso positivo.
"""

from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, Table, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.engine.reflection import Inspector

from core.db import dispose_engine, get_engine
from core.logging import configure_logging, get_logger
from domain.orm import Base

log = get_logger(__name__)

#: Alembic la mantiene ella misma; no está en el metadata del ORM.
TABLAS_IGNORADAS = frozenset({"alembic_version"})

#: Cómo nombra `_enum_check()` las restricciones que deriva de un enum. Sólo
#: ésas se comparan por valores: las demás (`tasa_nominal >= 0`) no son listas.
_PATRON_ENUM = re.compile(r"^ck_[a-z_]+_valido$")


def _valores_de(sql: str) -> frozenset[str]:
    """Los literales de un CHECK de enumeración, venga de donde venga.

    Sirve para las dos formas del mismo predicado: la del ORM,
    `fuente IN ('MANUAL', ...)`, y la que devuelve Postgres al leerlo,
    `(fuente)::text = ANY (ARRAY['MANUAL'::character varying, ...])`. En las dos
    lo único entrecomillado son los valores — los `::tipo` van sin comillas—,
    así que basta con quedarse con eso y comparar conjuntos.
    """
    return frozenset(re.findall(r"'([^']*)'", sql))


@dataclass(slots=True)
class ReporteEsquema:
    """Las diferencias encontradas. Vacío = la base está como debe."""

    tablas_faltantes: list[str] = field(default_factory=list)
    tablas_sobrantes: list[str] = field(default_factory=list)
    columnas_faltantes: list[str] = field(default_factory=list)
    columnas_sobrantes: list[str] = field(default_factory=list)
    nulabilidad: list[str] = field(default_factory=list)
    enumeraciones: list[str] = field(default_factory=list)
    revision_bd: str | None = None
    revision_head: str | None = None

    @property
    def migraciones_al_dia(self) -> bool:
        return self.revision_bd == self.revision_head

    @property
    def ok(self) -> bool:
        return self.migraciones_al_dia and not self.diferencias

    @property
    def diferencias(self) -> list[str]:
        return [
            *(f"falta la tabla {t}" for t in self.tablas_faltantes),
            *(f"sobra la tabla {t} (no está en el ORM)" for t in self.tablas_sobrantes),
            *(f"falta la columna {c}" for c in self.columnas_faltantes),
            *(f"sobra la columna {c} (no está en el ORM)" for c in self.columnas_sobrantes),
            *self.nulabilidad,
            *self.enumeraciones,
        ]

    def render(self) -> str:
        lineas = [
            f"  revisión en la base   {self.revision_bd or '(ninguna)'}",
            f"  revisión en el disco  {self.revision_head or '(ninguna)'}",
        ]
        if not self.migraciones_al_dia:
            lineas.append("  ✗ hay migraciones sin aplicar")
        if self.diferencias:
            lineas.append(f"  ✗ {len(self.diferencias)} diferencias de esquema:")
            lineas.extend(f"      - {d}" for d in self.diferencias)
        if self.ok:
            lineas.append("  ✓ el esquema real coincide con el del ORM")
        return "\n".join(lineas)


def _raiz_alembic() -> Path:
    """Dónde vive `alembic/`: primero el directorio de trabajo, luego el repo.

    Instalado, este módulo está en `site-packages`, así que subir dos niveles
    apunta a `/usr/local/lib/python3.12` y ahí no hay migraciones — el gate
    reventaba justo en el único sitio donde tiene que correr. En el contenedor
    el `WORKDIR` es `/app`, que es donde la imagen las copia.
    """
    cwd = Path.cwd()
    if (cwd / "alembic").is_dir():
        return cwd
    return Path(__file__).resolve().parents[2]


def _head_del_disco() -> str | None:
    """La revisión más reciente de `alembic/versions`, sin tocar la base."""
    raiz = _raiz_alembic()
    config = Config(str(raiz / "alembic.ini"))
    config.set_main_option("script_location", str(raiz / "alembic"))
    head = ScriptDirectory.from_config(config).get_current_head()
    # Explícito porque el mypy del pre-commit no instala alembic y ahí
    # `get_current_head()` es `Any`; en el entorno de desarrollo sí lo tipa.
    return str(head) if head is not None else None


def _comparar(conn: Connection, reporte: ReporteEsquema) -> None:
    """Compara el esquema real contra `Base.metadata`. Síncrono a propósito.

    El inspector de SQLAlchemy no tiene API async: se ejecuta con `run_sync`
    sobre una conexión async, que es el patrón que la propia librería propone.
    """
    inspector = inspect(conn)
    reales = set(inspector.get_table_names()) - TABLAS_IGNORADAS
    esperadas = set(Base.metadata.tables) - TABLAS_IGNORADAS

    reporte.tablas_faltantes = sorted(esperadas - reales)
    reporte.tablas_sobrantes = sorted(reales - esperadas)

    for nombre in sorted(esperadas & reales):
        tabla = Base.metadata.tables[nombre]
        columnas_reales = {c["name"]: c for c in inspector.get_columns(nombre)}
        columnas_esperadas = {c.name: c for c in tabla.columns}

        reporte.columnas_faltantes += [
            f"{nombre}.{c}" for c in sorted(set(columnas_esperadas) - set(columnas_reales))
        ]
        reporte.columnas_sobrantes += [
            f"{nombre}.{c}" for c in sorted(set(columnas_reales) - set(columnas_esperadas))
        ]

        for columna in sorted(set(columnas_esperadas) & set(columnas_reales)):
            esperada = columnas_esperadas[columna].nullable
            real = bool(columnas_reales[columna]["nullable"])
            if esperada != real:
                # Es la diferencia que más duele en caliente: un NOT NULL que
                # la base no tiene deja pasar filas que el ORM da por completas.
                reporte.nulabilidad.append(
                    f"{nombre}.{columna}: el ORM la quiere "
                    f"{'nullable' if esperada else 'NOT NULL'} y la base la tiene "
                    f"{'nullable' if real else 'NOT NULL'}"
                )

        _comparar_enumeraciones(inspector, tabla, reporte)


def _comparar_enumeraciones(inspector: Inspector, tabla: Table, reporte: ReporteEsquema) -> None:
    """Que la base acepte los valores que el enum del código ya usa.

    Se comprueba en la dirección que rompe: un miembro nuevo del enum que la
    base todavía rechaza. Al revés —la base acepta uno que el enum ya retiró—
    no rompe nada en caliente, y avisarlo obligaría a una migración por cada
    valor obsoleto que nadie escribe ya.
    """
    reales: dict[str, str] = {}
    for reflejada in inspector.get_check_constraints(tabla.name):
        nombre_real = reflejada.get("name")
        if isinstance(nombre_real, str):
            reales[nombre_real] = reflejada["sqltext"]

    for con in tabla.constraints:
        if not isinstance(con, CheckConstraint) or not isinstance(con.name, str):
            continue
        if not _PATRON_ENUM.match(con.name):
            continue

        if con.name not in reales:
            reporte.enumeraciones.append(f"{tabla.name}: falta la restricción {con.name}")
            continue

        faltan = _valores_de(str(con.sqltext)) - _valores_de(reales[con.name])
        if faltan:
            reporte.enumeraciones.append(
                f"{tabla.name}.{con.name}: la base rechaza {sorted(faltan)}, que el ORM ya "
                f"usa. Falta la migración que actualice la restricción."
            )


async def comprobar_esquema() -> ReporteEsquema:
    """Compara base real contra ORM y contra el head de las migraciones."""
    reporte = ReporteEsquema(revision_head=_head_del_disco())

    async with get_engine().connect() as conn:
        fila = await conn.execute(
            text(
                "SELECT version_num FROM alembic_version"
                " WHERE EXISTS (SELECT 1 FROM information_schema.tables"
                " WHERE table_name = 'alembic_version')"
            )
        )
        reporte.revision_bd = fila.scalar()
        await conn.run_sync(_comparar, reporte)

    return reporte


def main() -> int:
    configure_logging()

    async def _main() -> ReporteEsquema:
        try:
            return await comprobar_esquema()
        finally:
            await dispose_engine()

    try:
        reporte = asyncio.run(_main())
    except Exception as exc:  # noqa: BLE001 — es un gate: reporta y aborta
        log.error("schema_check_error", error=str(exc))
        print(f"Error al comprobar el esquema: {exc}", file=sys.stderr)
        return 1

    print("Comprobación de esquema:")
    print(reporte.render())

    if reporte.ok:
        log.info("schema_check_ok", revision=reporte.revision_bd)
        return 0

    log.error(
        "schema_check_failed",
        revision_bd=reporte.revision_bd,
        revision_head=reporte.revision_head,
        diferencias=len(reporte.diferencias),
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
