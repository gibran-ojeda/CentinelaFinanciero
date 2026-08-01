"""Alta manual de tasas desde CSV.

Es la vía de carga del MVP: la fase conceptual F1 opera con datos manuales
actualizados semanalmente. Cuando lleguen las ingestas automáticas (fases 7-9)
seguirá siendo la vía de corrección y de alta de instituciones nuevas.

Semántica **append-only**: cada fila del CSV es una observación. Nunca se
modifica ni se borra una tasa anterior — la vigente de un producto es la más
reciente en estado VIGENTE. Reimportar el mismo CSV no duplica nada porque la
clave natural es (producto, fecha_dato, fuente).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from api.services import cache
from api.services.tasas_vigentes import tasas_vigentes_por_producto
from core.db import session_scope
from core.logging import get_logger
from core.settings import settings
from domain.enums import EstadoTasa, FuenteTasa
from domain.orm import FuenteTasas, Institucion, Producto, Tasa
from rates_agent import pipeline
from rates_agent.pipeline import ReporteCorrida
from scheduler.bitacora import registrar_corrida

#: Id propio para las corridas disparadas desde la terminal. Con el mismo id
#: que el job del lunes, la pasada local con navegador y la del VPS se pisaban
#: en `job_runs`: `cli revisiones list` sólo miraba «la última corrida» y los
#: huecos de una borraban los de la otra.
JOB_ID_FETCH_MANUAL = "tasas_fetch_manual"

log = get_logger(__name__)

COLUMNAS_REQUERIDAS = {"producto_slug", "tasa_nominal", "fecha_dato"}

#: Una tasa por encima de esto casi seguro es un error de captura (un 950 en
#: vez de 9.50). Se rechaza la fila en vez de publicar un disparate.
TASA_MAXIMA_PLAUSIBLE = Decimal("100")


class ImportError_(Exception):
    """CSV mal formado o con filas no procesables."""


@dataclass(slots=True)
class ImportReport:
    creadas: int = 0
    duplicadas: int = 0
    errores: list[str] = field(default_factory=list)
    por_estado: dict[str, int] = field(default_factory=dict)

    def render(self) -> str:
        lineas = [
            f"  altas nuevas         {self.creadas:>4}",
            f"  ya existentes        {self.duplicadas:>4}",
        ]
        for estado, total in sorted(self.por_estado.items()):
            lineas.append(f"  en estado {estado:<12} {total:>4}")
        if self.errores:
            lineas.append(f"  filas rechazadas     {len(self.errores):>4}")
            lineas.extend(f"    - {e}" for e in self.errores)
        return "\n".join(lineas)


def _decimal(raw: str, campo: str, fila: int) -> Decimal | None:
    valor = (raw or "").strip()
    if not valor:
        return None
    try:
        return Decimal(valor)
    except InvalidOperation as exc:
        raise ImportError_(f"fila {fila}: '{campo}' no es un número ('{valor}')") from exc


def _fecha(raw: str, fila: int) -> date:
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ImportError_(f"fila {fila}: 'fecha_dato' debe ser YYYY-MM-DD ('{raw}')") from exc


async def import_csv(path: Path, *, dry_run: bool = False) -> ImportReport:
    """Da de alta las observaciones del CSV. Una transacción: todo o nada."""
    if not path.exists():
        raise ImportError_(f"no existe el archivo {path}")

    with path.open(encoding="utf-8", newline="") as handle:
        filas = list(csv.DictReader(handle))

    if not filas:
        raise ImportError_(f"{path.name} no tiene filas")

    faltantes = COLUMNAS_REQUERIDAS - set(filas[0])
    if faltantes:
        raise ImportError_(f"{path.name}: faltan columnas {sorted(faltantes)}")

    report = ImportReport()

    async with session_scope() as session:
        productos = {row.slug: row for row in (await session.execute(select(Producto))).scalars()}
        existentes = {
            (row.producto_id, row.fecha_dato, row.fuente)
            for row in (await session.execute(select(Tasa))).scalars()
        }

        for numero, fila in enumerate(filas, start=2):
            slug = (fila.get("producto_slug") or "").strip()
            if not slug or slug.startswith("#"):
                continue

            producto = productos.get(slug)
            if producto is None:
                report.errores.append(f"fila {numero}: producto desconocido '{slug}'")
                continue

            tasa_nominal = _decimal(fila["tasa_nominal"], "tasa_nominal", numero)
            if tasa_nominal is None:
                report.errores.append(f"fila {numero}: 'tasa_nominal' vacía")
                continue
            if tasa_nominal < 0 or tasa_nominal > TASA_MAXIMA_PLAUSIBLE:
                report.errores.append(
                    f"fila {numero}: tasa fuera de rango plausible ({tasa_nominal}%). "
                    f"Se rechaza en vez de publicar un dato imposible."
                )
                continue

            fecha_dato = _fecha(fila["fecha_dato"], numero)
            if fecha_dato > date.today():
                report.errores.append(
                    f"fila {numero}: fecha_dato en el futuro ({fecha_dato.isoformat()})"
                )
                continue

            fuente = FuenteTasa((fila.get("fuente") or "MANUAL").strip() or "MANUAL")
            estado = EstadoTasa((fila.get("estado") or "VIGENTE").strip() or "VIGENTE")

            # La invariante del agregador, en el punto de escritura: un dato que
            # recopiló un tercero no puede quedar vigente, porque el sitio lo
            # publicaría afirmando una procedencia que no es suya. Se hace valer
            # aquí y no filtrando al leer, para que no dependa de que cada
            # consulta futura se acuerde de excluirlo.
            if fuente is FuenteTasa.AGREGADOR and estado is EstadoTasa.VIGENTE:
                report.errores.append(
                    f"fila {numero}: una tasa de fuente AGREGADOR no puede estar VIGENTE. "
                    f"Se publica lo que publica la institución, no lo que recopiló un "
                    f"tercero; el dato de agregador sólo sirve de contraste."
                )
                continue

            clave = (producto.id, fecha_dato, fuente)
            if clave in existentes:
                report.duplicadas += 1
                continue

            session.add(
                Tasa(
                    producto_id=producto.id,
                    tasa_nominal=tasa_nominal,
                    gat_nominal=_decimal(fila.get("gat_nominal", ""), "gat_nominal", numero),
                    gat_real=_decimal(fila.get("gat_real", ""), "gat_real", numero),
                    fecha_dato=fecha_dato,
                    fuente=fuente,
                    fuente_url=(fila.get("fuente_url") or "").strip() or None,
                    estado=estado,
                    notas=(fila.get("notas") or "").strip() or None,
                )
            )
            existentes.add(clave)
            report.creadas += 1
            report.por_estado[estado.value] = report.por_estado.get(estado.value, 0) + 1

        if dry_run:
            await session.rollback()

    # Siempre que el alta haya sido real, aunque no creara nada. Escribir tasas
    # sin invalidar deja al comparador sirviendo una respuesta que ya no
    # corresponde, y el caso que muerde no es el de las altas: es el del
    # despliegue. El script espera a que la API esté sana y **después** siembra,
    # así que entre una cosa y otra el healthcheck de `web` pide la portada, la
    # API la calcula vacía y la cachea cinco minutos. Un import que no invalida
    # deja el sitio en blanco todo ese rato — y hace fallar el gate de la
    # portada en un despliegue perfectamente bueno.
    if not dry_run:
        await cache.invalidar()

    log.info(
        "tasas_importadas",
        archivo=path.name,
        creadas=report.creadas,
        duplicadas=report.duplicadas,
        errores=len(report.errores),
        dry_run=dry_run,
    )
    return report


# ─── Lista de revisión ────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ProductoPendiente:
    """Un producto que no puede salir al sitio público, y por qué."""

    institucion: str
    producto_slug: str
    producto_nombre: str
    tasa_nominal: Decimal | None
    fecha_dato: date | None
    fuente_url: str | None
    motivo: str


@dataclass(slots=True)
class ListaRevision:
    """La lista de la semana, agrupada por institución.

    `urls_oficiales` viene de `fuentes_tasas`, que es el catálogo curado de
    páginas por institución. Es lo que hay que abrir para verificar, y no la
    `fuente_url` que trae la tasa — esa es de donde salió el dato la vez
    anterior, que muchas veces es justo el problema.
    """

    pendientes: list[ProductoPendiente] = field(default_factory=list)
    urls_oficiales: dict[str, list[tuple[str, bool]]] = field(default_factory=dict)

    def render(self) -> str:
        if not self.pendientes:
            return "  Nada pendiente: todas las tasas del catálogo están verificadas."

        lineas: list[str] = []
        # `key=str.casefold`: si no, "kubo.financiero" cae al final de la lista,
        # detrás de "Ualá", y quien la recorre se lo salta.
        for institucion in sorted({p.institucion for p in self.pendientes}, key=str.casefold):
            lineas.append(f"\n  {institucion}")
            for url, requiere_js in self.urls_oficiales.get(institucion, []):
                marca = "  [requiere JS: ábrela en el navegador]" if requiere_js else ""
                lineas.append(f"    → {url}{marca}")
            if not self.urls_oficiales.get(institucion):
                lineas.append("    → (sin URL curada en fuentes_tasas.yaml)")
            for p in sorted(self.pendientes, key=lambda x: x.producto_slug):
                if p.institucion != institucion:
                    continue
                tasa = f"{p.tasa_nominal}%" if p.tasa_nominal is not None else "—"
                fecha = p.fecha_dato.isoformat() if p.fecha_dato else "—"
                lineas.append(f"      {p.producto_slug:<26} {tasa:>8}  {fecha:>10}  {p.motivo}")

        total = len(self.pendientes)
        instituciones = len({p.institucion for p in self.pendientes})
        lineas.append(
            f"\n  {total} productos en {instituciones} instituciones. "
            f"Actualiza seeds/tasas.csv con lo que publique cada página "
            f"(estado VIGENTE, fuente MANUAL, la URL de la institución) y corre "
            f"`python -m cli tasas import seeds/tasas.csv`."
        )
        return "\n".join(lineas)


async def listar_pendientes() -> ListaRevision:
    """Qué falta verificar para que el catálogo pueda salir a internet.

    Dos cosas distintas caen aquí y las dos importan: los productos cuya tasa
    está en `PENDIENTE_REVISION` —hoy leída de un agregador y no de la propia
    institución— y los que no tienen ninguna tasa. Ambos son invisibles en el
    sitio público, así que ambos son trabajo de la misma sesión.
    """
    lista = ListaRevision()

    async with session_scope() as session:
        productos = (
            (
                await session.execute(
                    select(Producto)
                    .options(selectinload(Producto.institucion))
                    .order_by(Producto.slug)
                )
            )
            .scalars()
            .all()
        )
        vigentes = await tasas_vigentes_por_producto(session, incluir_pendientes=True)

        for producto in productos:
            tasa = vigentes.get(producto.id)
            if tasa is not None and tasa.estado is EstadoTasa.VIGENTE:
                continue
            lista.pendientes.append(
                ProductoPendiente(
                    institucion=producto.institucion.nombre,
                    producto_slug=producto.slug,
                    producto_nombre=producto.nombre,
                    tasa_nominal=tasa.tasa_nominal if tasa else None,
                    fecha_dato=tasa.fecha_dato if tasa else None,
                    fuente_url=tasa.fuente_url if tasa else None,
                    motivo="sin verificar" if tasa else "sin tasa",
                )
            )

        interesantes = {p.institucion for p in lista.pendientes}
        fuentes = (
            await session.execute(
                select(FuenteTasas, Institucion.nombre)
                .join(Institucion, Institucion.id == FuenteTasas.institucion_id)
                .where(FuenteTasas.activa.is_(True))
                .order_by(Institucion.nombre, FuenteTasas.url)
            )
        ).all()
        for fuente, nombre in fuentes:
            if nombre in interesantes:
                lista.urls_oficiales.setdefault(nombre, []).append(
                    (fuente.url, fuente.requiere_js)
                )

    log.info("tasas_pendientes_listadas", productos=len(lista.pendientes))
    return lista


# ─── Retiro de filas de agregador sustituidas ─────────────────


@dataclass(slots=True)
class ReporteRetiro:
    """Qué filas del CSV ya cumplieron su función de contraste."""

    #: `(slug, fuente que la sustituyó, fecha de esa lectura)`.
    retiradas: list[tuple[str, str, str]] = field(default_factory=list)
    conservadas: int = 0
    desconocidas: list[str] = field(default_factory=list)
    dry_run: bool = False

    def render(self) -> str:
        if not (self.retiradas or self.conservadas or self.desconocidas):
            return "  (ninguna fila de agregador en el CSV)"
        lineas = []
        for slug, fuente, fecha in self.retiradas:
            lineas.append(f"  retirada    {slug:<26} sustituida por {fuente} del {fecha}")
        lineas.append(f"  conservadas {self.conservadas:>4}  (aún sin lectura oficial)")
        if self.desconocidas:
            lineas.append(
                f"  ⚠ slugs del CSV que el catálogo no conoce: {', '.join(self.desconocidas)}"
            )
        if self.dry_run:
            lineas.append("  (simulación: el archivo no se tocó)")
        return "\n".join(lineas)


async def retirar_sustituidas(path: Path, *, dry_run: bool = False) -> ReporteRetiro:
    """Comenta las filas AGREGADOR cuyo producto ya tiene lectura oficial.

    Es la promesa del encabezado del propio CSV — «cada una se retira en
    cuanto su lectura oficial la sustituye» — hecha comando. Retirar es
    **comentar**, no borrar: los dos lectores (`import_csv` y el seed) ya
    ignoran las líneas con `#`, y la línea original queda a la vista con la
    razón del retiro. La base no se toca: las observaciones importadas son
    historia append-only y la ventana de vigencia ya prefiere la oficial.
    """
    reporte = ReporteRetiro(dry_run=dry_run)
    crudo = path.read_text(encoding="utf-8")
    termina_en_salto = crudo.endswith("\n")
    lineas = crudo.splitlines()

    encabezado: list[str] | None = None
    candidatas: dict[int, str] = {}  # índice de línea → producto_slug
    for indice, linea in enumerate(lineas):
        celdas = next(csv.reader([linea]), [])
        if not celdas or not celdas[0].strip() or celdas[0].strip().startswith("#"):
            continue
        if encabezado is None:
            encabezado = [c.strip() for c in celdas]
            continue
        fila = dict(zip(encabezado, (c.strip() for c in celdas), strict=False))
        if fila.get("fuente") == FuenteTasa.AGREGADOR.value:
            candidatas[indice] = fila.get("producto_slug", "")

    if not candidatas:
        return reporte

    async with session_scope() as session:
        productos = {
            slug: pid
            for slug, pid in (
                await session.execute(
                    select(Producto.slug, Producto.id).where(
                        Producto.slug.in_(set(candidatas.values()))
                    )
                )
            )
            .tuples()
            .all()
        }
        vigentes = await tasas_vigentes_por_producto(
            session, list(productos.values()), incluir_pendientes=True
        )

    hoy = date.today().isoformat()
    for indice, slug in candidatas.items():
        producto_id = productos.get(slug)
        if producto_id is None:
            reporte.desconocidas.append(slug)
            continue
        ganadora = vigentes.get(producto_id)
        # AGREGADOR jamás puede ser VIGENTE, así que la segunda condición está
        # implícita en la primera; se deja explícita porque es el criterio.
        sustituida = (
            ganadora is not None
            and ganadora.estado is EstadoTasa.VIGENTE
            and ganadora.fuente is not FuenteTasa.AGREGADOR
        )
        if not sustituida:
            reporte.conservadas += 1
            continue
        lineas[indice] = (
            f"# retirada {hoy} (sustituida por {ganadora.fuente.value} "
            f"{ganadora.fecha_dato.isoformat()}): {lineas[indice]}"
        )
        reporte.retiradas.append((slug, ganadora.fuente.value, ganadora.fecha_dato.isoformat()))

    if reporte.retiradas and not dry_run:
        path.write_text(
            "\n".join(lineas) + ("\n" if termina_en_salto else ""),
            encoding="utf-8",
            newline="\n",
        )

    return reporte


# ─── Lectura automática ───────────────────────────────────────


#: Backoff temporal para una corrida interactiva. Los 300 y 1200 segundos que
#: usa el job están calibrados para algo desatendido a las seis de la mañana;
#: delante de una terminal son veinticinco minutos mirando un cursor. Se
#: reintenta una vez, corto, y lo que no salga se reporta para la próxima.
ESPERAS_INTERACTIVAS: tuple[float, ...] = (20.0,)


async def correr_fetch(
    *,
    solo_navegador: bool = False,
    sin_navegador: bool = False,
    esperas_backoff_s: tuple[float, ...] | None = None,
) -> ReporteCorrida:
    """Corre el pipeline de lectura desde la terminal.

    Es el mismo código que ejecuta el job semanal: si fueran dos, el que se
    corre a mano acabaría comportándose distinto del que corre solo, y el
    reparto entre el VPS y la máquina local dejaría de ser una decisión de
    dónde ejecutar para convertirse en dos implementaciones.

    Lo único que cambia es la **espera**, y por una razón de quién mira: un job
    desatendido puede permitirse esperar veinte minutos a que un sitio deje de
    limitar; una persona en una terminal, no.

    Con `--solo-navegador` se levanta Chromium; sin eso basta el cliente HTTP
    plano y no hace falta tener Playwright instalado.
    """
    solo_js: bool | None = True if solo_navegador else (False if sin_navegador else None)
    agente = settings.fetch_user_agent
    esperas = ESPERAS_INTERACTIVAS if esperas_backoff_s is None else esperas_backoff_s

    from rates_agent.fetcher import Fetcher, TransporteHttpx

    transportes: list[Any] = [TransporteHttpx(user_agent=agente)]
    if solo_js is not False:
        # El navegador sólo se arma cuando puede hacer falta: importar
        # playwright sin tenerlo instalado revienta el comando entero.
        from rates_agent.navegador import TransporteNavegador

        transportes.append(TransporteNavegador(user_agent=agente))

    fetcher = Fetcher(transportes, esperas_backoff_s=esperas)

    # La corrida deja su fila en `job_runs` aunque la dispare una persona —
    # una pasada local es una corrida igual de real que la del lunes— pero
    # bajo su propio id: `cli revisiones list` agrega los huecos de las
    # corridas recientes de ambos, y así ninguna borra lo que vio la otra.
    async with registrar_corrida(JOB_ID_FETCH_MANUAL) as corrida:
        reporte = await pipeline.correr(fetcher=fetcher, solo_requieren_js=solo_js)
        corrida.metricas.update(reporte.como_metricas())
        corrida.metricas["disparada_por"] = "cli"
        if reporte.fracaso_total:
            # Fallida en la bitácora, pero sin lanzar: la persona delante de
            # la terminal todavía recibe el render con la primera causa.
            corrida.fallar(f"las {reporte.fuentes} fuentes fallaron")
    return reporte


__all__ = [
    "JOB_ID_FETCH_MANUAL",
    "TASA_MAXIMA_PLAUSIBLE",
    "ImportReport",
    "ImportError_",
    "ListaRevision",
    "ProductoPendiente",
    "ReporteRetiro",
    "correr_fetch",
    "import_csv",
    "listar_pendientes",
    "retirar_sustituidas",
]
