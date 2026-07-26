# Fase 2 — Modelo de datos y configuración

## Objetivo

Esquema completo con migraciones, ConfigStore de dos capas operativo y datos semilla cargables por CLI. Al cerrar esta fase, la base de datos puede contener el catálogo MVP completo (instituciones, productos, tasas manuales) y los parámetros de negocio son ajustables sin deploy.

## Entregables

- `src/domain/`:
  - `enums.py` — `CategoriaInstitucion` (GOBIERNO, SOFIPO, BANCO_DIGITAL, BANCO_TRADICIONAL, IFPE), `TipoSeguro` (SOBERANO, IPAB, PROSOFIPO, NINGUNO), `TipoProducto` (VISTA, PLAZO), `FuenteTasa` (MANUAL, BANXICO_API, CNBV, FETCH_DIRIGIDO, LLM_RESEARCH), `EstadoTasa` (VIGENTE, PENDIENTE_REVISION, RECHAZADA), `Severidad` (AMARILLA, ROJA).
  - `orm.py` — modelos SQLAlchemy 2.0 (declarative, tipado con `Mapped[]`).
  - `models.py` — modelos pydantic de dominio con `.to_domain()` desde ORM (separación explícita, patrón NarrativeAlpha).
- **Migraciones Alembic** (`alembic/`) — configuradas contra el metadata del ORM; primera migración crea todo el esquema.
- `src/core/config_store.py` — segunda capa de configuración: tabla `config_store` + versiones, `ConfigKeySpec(settings_attr, value_type, group, description)` como registry de qué parámetros migran a BD, snapshot inmutable en memoria con TTL, y proxy síncrono `effective` con los mismos nombres de atributo que `Settings` (migrar un consumidor = swap `settings.x` → `effective.x`).
- `seeds/` — `instituciones.yaml`, `productos.yaml`, `tasas.csv` (plantilla con columnas: institución, producto, tasa_nominal, gat_nominal, gat_real, fecha_dato, fuente_url), `indicadores.csv`, `parametros_fiscales.yaml`, `fuentes_tasas.yaml` (URLs curadas por institución para la fase 9).
- `src/cli/` — `__main__.py` con subcomandos: `python -m cli seed` (carga idempotente de catálogos), `python -m cli tasas import <csv>` (alta de tasas manuales con validación), `python -m cli config list|set` (inspección del ConfigStore).

## Esquema (tablas y columnas principales)

| Tabla | Columnas clave |
|---|---|
| `instituciones` | id, nombre, nombre_cnbv (para mapeo fase 8), categoria, tipo_seguro, estatus_regulatorio, url_sitio, activa |
| `productos` | id, institucion_id FK, nombre, tipo (VISTA/PLAZO), plazo_dias (null si vista), monto_minimo, liquidez, penalizacion_retiro, activo |
| `tasas` | id, producto_id FK, tasa_nominal, gat_nominal?, gat_real?, fecha_dato, fuente, fuente_url?, estado, created_at — **append-only**: una fila por observación; la vigente es la más reciente en estado VIGENTE |
| `indicadores_financieros` | id, institucion_id FK, periodo (date), imor, icap, icor, nicap_nivel, captacion, cartera_total, fuente_url — unique (institucion_id, periodo) |
| `banderas` | id, institucion_id FK, tipo, severidad, motivo, periodo_dato, activa, created_at |
| `series_economicas` | id, clave_banxico, nombre, unidad / `valores_serie`: serie_id FK, fecha, valor — unique (serie_id, fecha) |
| `parametros_fiscales` | id, anio, tasa_retencion_capital, notas — el tratamiento por tipo de instrumento vive en `src/metrics/fiscal.py` parametrizado por esta tabla |
| `fuentes_tasas` | id, institucion_id FK, url, nivel (2/3), requiere_js, activa, ultima_extraccion_at |
| `revisiones_tasas` | id, tasa_id FK, motivo, valor_anterior, valor_nuevo, estado (PENDIENTE/APROBADA/RECHAZADA), revisor, resuelto_at |
| `config_store` / `config_versions` | key, value, value_type, group, version activa, historial |
| `job_runs` | id, job_id, inicio, fin, estado, metricas (JSONB), error |

Reglas de diseño (de §16 del foundation): límites de seguro **en UDIs** (constantes de dominio: IPAB=400,000, PROSOFIPO=25,000) convertidos a MXN con la serie UDI; nunca borrar tasas — se supersede con una fila nueva.

## Tareas

1. Escribir enums y ORM; generar la migración inicial con Alembic y verificarla desde BD vacía.
2. Implementar ConfigStore: registry inicial con los parámetros de banderas (umbral_imor_amarilla=3.0, umbral_imor_roja=6.0, umbral_icap_amarilla=15.0, umbral_icap_roja=10.5, umbral_cobertura_amarilla=100.0, umbral_cobertura_roja=70.0, umbral_gat_inconsistencia_pp=1.5) y tolerancias de revisión (fase 9). Grupos: `banderas`, `fiscal`, `revision`, `scheduler`.
3. Poblar `seeds/` con el **catálogo MVP completo** (decisión D3 resuelta): **top 10 SOFIPOs** (DiDi, FinSUS, Kubo Financiero, Libertad, Caja Pop Mexicana, Te Creemos y las demás con operación activa), **top 5 neobancos con licencia bancaria** (de la tabla de §3.3: Nu México, Revolut, Ualá, OpenBank, Hey Banco, Bineo, Plata — seleccionar 5 por relevancia de captación), **CETES** (28/91/182/364 días) y **BONDDIA**. Datos reales al día de la carga, cada tasa con su `fuente_url` y `fecha_dato`.
4. Implementar la CLI con carga idempotente (upsert por clave natural: nombre de institución, producto+plazo).
5. Registrar en `job_runs` la ejecución del `heartbeat` de fase 1 (cerrar el pendiente).
6. Tests: modelos y constraints con testcontainers (Postgres real); ConfigStore (snapshot, TTL, proxy `effective`, fallback a `Settings`); CLI idempotente (correr seed dos veces no duplica).

## Criterios de aceptación

- [ ] `alembic upgrade head` construye el esquema completo desde BD vacía; `alembic downgrade -1` es reversible.
- [ ] `python -m cli seed` seguido de una segunda ejecución no crea duplicados.
- [ ] `python -m cli tasas import seeds/tasas.csv` da de alta tasas VIGENTES con `fecha_dato` y fuente MANUAL.
- [ ] `effective.umbral_imor_roja` lee el valor de BD si existe override y cae a `Settings` si no; el cambio vía `python -m cli config set` se refleja sin reiniciar (tras TTL).
- [ ] Tests con testcontainers en verde en CI.

## Dependencias

Fase 1 (core, compose, CI).
