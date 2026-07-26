# Fase 4 — API pública

## Objetivo

API interna completa que sirve el comparador, el detalle de institución y la calculadora usando los datos semilla de la fase 2 y el motor de la fase 3. "Pública" en contrato (es la API del producto), pero **nunca expuesta a internet**: solo el BFF (fase 5) y el admin la consumen con `X-API-Key`.

## Entregables

- Routers en `src/api/routers/`:
  - `comparador.py` — `GET /api/v1/comparador` con **todos** los filtros de §7: `plazo` (VISTA, 28, 91, 182, 364, 365+), `categoria`, `monto` (excluye productos con monto_minimo mayor), `seguro` (solo IPAB / solo SOBERANO / todos), `liquidez`, `sin_banderas` (bool), `orden` (tasa_nominal | ten | gat | cobertura). Cada fila: institución, producto, tasa nominal, TEN, GAT (publicada o equivalente, marcado cuál), cobertura de seguro en MXN (vía UDI vigente), banderas activas, `fecha_dato`, `fuente`.
  - `instituciones.py` — `GET /api/v1/instituciones/{id}`: detalle + productos + indicadores del último periodo + banderas activas e históricas + capa de profundidad de §11 (la UI decide qué mostrar; la API entrega todo con su periodo).
  - `calculadora.py` — `POST /api/v1/calculadora`: `{monto, plazo_dias, producto_id | lista}` → `DesgloseCascada` por producto (5 conceptos de §6) + **nota fiscal** (qué retención se aplicó y fecha de referencia) + disclaimer.
  - `meta.py` — `GET /api/v1/meta/frescura`: última actualización por fuente de datos (obligación de §11).
  - `admin.py` — con `X-API-Key` de admin (distinta a la del BFF): `POST /admin/tasas` (alta manual), `GET /admin/revisiones` y `POST /admin/revisiones/{id}` (aprobar/rechazar — la cola se llena en fase 9, el contrato queda listo aquí).
- `src/api/dependencies.py` — auth por `X-API-Key` (dos niveles: lectura BFF, escritura admin), sesión de BD, `effective` config.
- Cache Redis de la vista comparador (llave por combinación de filtros, TTL configurable en ConfigStore, invalidación al escribir tasas o banderas).
- Job `banderas_recompute` en el scheduler: recorre `indicadores_financieros` + tasas vigentes, ejecuta `metrics.flags.evaluar_banderas` y sincroniza la tabla `banderas`. Trigger: tras cada ingesta y diario. Doble gate (flag env + kill-switch ConfigStore).
- Esquemas de respuesta pydantic en `src/api/schemas.py` — el contrato OpenAPI es el contrato del BFF y del MCP futuro.

## Tareas

1. Definir `schemas.py` primero (contrato antes que implementación) y revisar que cubre todo lo que la UI de §11 necesita en cada capa de profundidad.
2. Implementar dependencias (auth, sesión, config) y los routers en orden: meta → instituciones → comparador → calculadora → admin.
3. Implementar el cálculo de la vista comparador como servicio (`src/api/services/comparador.py`): join productos+tasas vigentes+banderas, métricas vía fase 3, orden y filtros en SQL donde sea posible.
4. Implementar cache Redis con invalidación; medir con y sin cache.
5. Implementar `banderas_recompute` y registrarlo en `JOBS_REGISTRY`; escribir su resultado en `job_runs`.
6. Tests de integración (testcontainers + seed de fase 2): un test por filtro de §7, combinaciones de filtros, orden estable, calculadora contra los ejemplos del foundation, auth (sin key → 401; key de lectura no puede escribir), invalidación de cache.

## Criterios de aceptación

- [ ] OpenAPI (`/docs`) documenta todos los endpoints con ejemplos.
- [ ] Cada filtro de §7 tiene al menos un test de integración que verifica inclusión Y exclusión.
- [ ] **Toda** respuesta que contenga una tasa incluye `fecha_dato` y `fuente`; toda respuesta de calculadora incluye la nota fiscal y el disclaimer.
- [ ] `sin_banderas=true` excluye instituciones con cualquier bandera activa; el orden por `gat` usa la publicada y cae a la equivalente calculada (marcada como tal).
- [ ] El comparador con cache responde < 50ms en local con el seed completo.
- [ ] `banderas_recompute` es idempotente: dos corridas seguidas dejan el mismo estado.

## Dependencias

Fases 2 (esquema y seeds) y 3 (motor de métricas).
