# Fase 10 — Extensiones

## Objetivo

Bloques de valor **independientes entre sí**, priorizables por separado una vez que el núcleo (fases 1–9) opera en producción. Cubren las fases conceptuales F5 (cobertura completa) y F6 (contexto avanzado) del foundation, más el MCP server. Cada bloque tiene su propio corte de aceptación; no hay orden obligatorio entre ellos.

## Bloque A — Cobertura completa (F5)

**Qué:** incorporar al comparador BONOS M, UDIBONOS y BONDES D (los datos ya llegan por la fase 7 — falta materializarlos como productos comparables) y el PRLV de banca tradicional (BBVA, Banorte, Santander, HSBC, Scotiabank... — llegan por el pipeline de la fase 9 añadiendo sus URLs a `fuentes_tasas`).

**Tareas:** productos gubernamentales de plazo largo en el catálogo con su tratamiento fiscal correcto (especial UDIBONOS, ya implementado en fase 3); sección de la UI para plazos > 1 año; alta de fuentes PRLV y calibración de extracción.

**Aceptación:** un usuario puede comparar un CETE vs una SOFIPO vs un PRLV vs un UDIBONO en la misma tabla con TEN homologada — el gap central de §8 del foundation cerrado.

## Bloque B — Histórico y tendencias (F6)

**Qué:** las tablas ya son append-only (`tasas`, `indicadores_financieros`, `valores_serie`); explotarlas: endpoints `GET /api/v1/instituciones/{id}/historico` y gráficas de evolución de tasa por producto y de IMOR/ICAP por institución en la página de detalle.

**Tareas:** endpoints con agregación por periodo; componente de gráfica en el frontend (isla React; librería ligera de charts); tendencia visible en el detalle ("el IMOR de X subió 3 periodos seguidos").

**Aceptación:** detalle de institución muestra 12+ meses de historia de tasas e indicadores con su fuente.

## Bloque C — Alertas (F6)

**Qué:** suscripciones a cambios: "avísame si la tasa de X baja", "si Y recibe una bandera", "si aparece una tasa mejor a N días de plazo".

**Tareas:** tabla `suscripciones` (email, tipo de alerta, criterios, confirmación double opt-in); job `alertas_evaluar` tras cada ingesta; envío por email (SMTP/servicio transaccional); páginas de alta/baja; privacidad (emails fuera de logs, baja en un clic).

**Aceptación:** ciclo completo real: suscripción → cambio de dato en ingesta → email recibido → baja funciona.

## Bloque D — Simulador de cartera (F6)

**Qué:** extensión de la calculadora: repartir un monto entre varios instrumentos (ej. 50% CETES 28d + 30% SOFIPO 91d + 20% BONDDIA) y ver el desglose en cascada agregado, respetando límites de cobertura por institución (avisar si una posición excede IPAB/PROSOFIPO).

**Tareas:** `POST /api/v1/simulador` (composición → cascada agregada + avisos de cobertura); UI de composición con la misma visual de cascada.

**Aceptación:** el simulador reproduce la suma de calculadoras individuales y señala excesos de cobertura correctamente.

## Bloque E — MCP server

**Qué:** exponer el comparador a asistentes LLM (Claude, etc.) como servidor MCP — mismo patrón que NarrativeAlpha: `src/mcp_server/` con FastMCP sobre HTTP, **cliente HTTP de la API interna** (no toca la BD), solo lectura.

**Tareas:** tools `comparar_instrumentos`, `detalle_institucion`, `calcular_rendimiento_real`, `frescura_datos`; instrucciones del servidor con el disclaimer (no es asesoría); servicio `mcp` en el compose de Centinela (puerto propio en loopback, p. ej. 8012 — el 8001 lo ocupa el MCP de NarrativeAlpha) más una ruta `handle /mcp*` en el site block compartido de `centinelafinanciero.cloud`; auth por bearer token.

**Aceptación:** desde un cliente MCP externo se puede preguntar "¿dónde rinde más $50,000 a 91 días con cobertura IPAB?" y la respuesta sale de la API con datos vigentes y disclaimer.

## Bloque F — SEO programático

**Qué:** páginas indexables generadas desde datos: `/comparar/cetes-vs-{sofipo}`, `/tasas/{institucion}/{plazo}`, `/mejores-tasas-{plazo}` — server-rendered con datos vigentes y fecha visible.

**Tareas:** rutas dinámicas en Astro desde el catálogo; sitemap automático; canonical y datos estructurados; texto generado con plantillas (no LLM en runtime) para evitar thin content.

**Aceptación:** páginas indexadas en Search Console con impresiones crecientes; sin penalización por contenido duplicado (canonicals correctos).

## Dependencias

Núcleo (fases 1–9) en producción. Los bloques son independientes entre sí; A y B son los de mayor valor/costo, E es el más barato si ya se replica el patrón de NarrativeAlpha.
