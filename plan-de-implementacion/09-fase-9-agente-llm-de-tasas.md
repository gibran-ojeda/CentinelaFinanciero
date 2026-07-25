# Fase 9 — Agente LLM de tasas (niveles 2 y 3)

## Objetivo

Automatizar las tasas que **no tienen API** (SOFIPOs, neobancos, PRLV, BONDDIA) con la estrategia de tres niveles de §15 del foundation: fetch dirigido + extracción LLM como mecanismo primario, búsqueda abierta con agente LLM solo para descubrimiento y verificación, y cola de revisión humana como control de calidad. El runbook manual de la fase 6 queda como fallback documentado — no se elimina.

> **Contexto de la decisión** (evaluada en la planificación): la API de DeepSeek no tiene búsqueda web nativa; el patrón adoptado es *tool-use* con ejecutor de búsqueda propio (SearXNG/ddgs), heredado de NarrativeAlpha. El scraping clásico por selectores CSS se descartó: 20+ scrapers por institución se rompen en silencio con cada rediseño. El LLM extrae de la página lo que un selector no sobreviviría.

## Entregables

### Infraestructura LLM — `src/llm/`
- `providers/base.py` — ABC `BaseLLMProvider` + jerarquía de errores (`LLMProviderError`, `LLMRateLimitError`, `LLMTimeoutError`, `LLMParseError`) + dataclass `LLMResponse` (content, tool_calls, finish_reason, tokens, cost).
- `providers/openai_compat.py` — un provider cubre DeepSeek/OpenAI/compatibles vía `AsyncOpenAI` con `base_url` intercambiable; tabla de pricing por modelo y cálculo de costo por llamada.
- `client.py` — router por tiers (`extraction` → modelo económico p.ej. `deepseek-chat`; `research` → el mismo u otro según calibración) con fallback y retry. **Nota heredada de NarrativeAlpha: no usar modelos razonadores en el tool-loop** (no emiten JSON confiable con tools activos).
- `cost_tracker.py` — acumulado diario en Redis con límite duro (`LLM_COST_DAILY_LIMIT_USD`, decisión D2; sugerido $1/día): al alcanzarlo, los jobs LLM no-opean y lo registran.
- `parsers.py` — `clean_llm_json` / `safe_parse_json(content, required_keys)`: quita fences markdown y bloques `<think>`, extrae el objeto JSON correcto.
- `prompts/` (carpeta raíz del repo) — plantillas `.md` externas cargadas con cache y renderizadas con `format_map`: `extract_rates_system.md`, `extract_rates_user.md`, `research_system.md`, `research_user.md`.

### Agente de tasas — `src/rates_agent/`
- `fetcher.py` — **nivel 2, parte determinista**: recorre `fuentes_tasas` activas; httpx (headers de navegador, timeout, retry) + trafilatura para extraer el texto principal; para fuentes con `requiere_js=true`, Playwright (extra `[browser]` del pyproject, instalado solo en la imagen del scheduler si se necesita). Guarda el contenido crudo con hash — si el hash no cambió desde la última extracción, **no llama al LLM** (ahorro directo).
- `extractor.py` — **nivel 2, parte LLM**: contenido de página → DeepSeek con prompt de extracción → JSON validado contra esquema pydantic `TasaExtraida {producto, tipo, plazo_dias, tasa_nominal, gat_nominal?, gat_real?, monto_minimo?, condiciones?, confianza}`. Rechaza respuestas que no validen; reintento con feedback del error de validación (1 vez).
- `researcher.py` — **nivel 3**: tool-use loop con tool `web_search`; `SearchExecutor` determinista con backends `ddgs` (primario, $0) y `searxng` (self-hosted, servicio nuevo en compose), con retry por backend → cadena de fallbacks → circuit breaker por corrida; **invariante anti-alucinación**: se acumulan las URLs devueltas por búsquedas reales (`allowed_urls`) y todo hallazgo cuyas URLs no estén en ese conjunto se descarta; tras N rondas se retiran los tools para forzar la respuesta JSON final. Uso: instituciones con fetch fallido, fuentes stale, y descubrimiento (¿nueva SOFIPO? ¿cambió la URL de tasas?).
- `reviewer.py` — control de calidad: compara cada `TasaExtraida` contra la tasa vigente del producto. Dentro de tolerancia (ConfigStore, p.ej. ±0.5pp) → publica directo como VIGENTE con `fuente_url` y fuente `FETCH_DIRIGIDO`/`LLM_RESEARCH`. Fuera de tolerancia, producto nuevo, o confianza baja → fila en estado `PENDIENTE_REVISION` + entrada en `revisiones_tasas`; **nunca se publica sola**.
- Flujo de aprobación: endpoints admin de la fase 4 (`GET/POST /admin/revisiones`) + `python -m cli revisiones list|approve|reject` (decisión D4: CLI al inicio).

### Jobs y servicios
- `tasas_fetch_dirigido` — lunes 06:00 CDMX: fetcher + extractor + reviewer sobre todas las `fuentes_tasas` activas.
- `tasas_research_abierta` — miércoles 06:00 CDMX: researcher **solo** sobre instituciones stale (sin dato fresco según SLA) o con fetch fallido dos veces.
- Servicio `searxng` en compose (interno, formato JSON habilitado).
- Ambos jobs con doble gate, lock Redis, `job_runs` con métricas (páginas procesadas, tokens, costo USD, tasas publicadas/en cola).

## Tareas

1. Implementar `src/llm/` (base → openai_compat → client → cost_tracker → parsers) con tests respx contra la forma de respuesta de la API de DeepSeek.
2. Escribir los prompts de extracción; calibrar contra 5–10 páginas reales del catálogo (guardadas como fixtures) hasta que la extracción sea estable.
3. Implementar fetcher con hash de contenido; catalogar en `fuentes_tasas` qué instituciones requieren JS.
4. Implementar extractor + reviewer + flujo de aprobación CLI.
5. Implementar SearchExecutor y el tool-loop del researcher con la invariante `allowed_urls`; prompt con reglas duras (solo URLs de resultados reales, ventana de fechas, JSON final obligatorio, "sin datos" es respuesta válida).
6. Añadir `searxng` al compose; registrar jobs con doble gate.
7. Corrida de calibración: 2–3 semanas midiendo tasa de aprobación de la cola, costo semanal y falsos (tasas mal extraídas que pasaron tolerancia); ajustar prompts y tolerancias en ConfigStore.

## Criterios de aceptación

- [ ] **Ninguna** tasa con fuente `LLM_RESEARCH` o `FETCH_DIRIGIDO` existe en estado VIGENTE sin `fuente_url` poblada; para research, la URL proviene verificablemente de `allowed_urls` (test de la invariante).
- [ ] Una extracción fuera de tolerancia termina en `revisiones_tasas` y NO cambia la tasa vigente hasta aprobarse.
- [ ] Si el contenido de una página no cambió (hash), el job no gasta tokens en ella.
- [ ] El CostTracker corta los jobs al llegar al límite diario y lo registra; el costo semanal medido queda por debajo del límite (esperado: centavos de USD).
- [ ] Tasa de aprobación de la cola ≥ 80% tras la calibración (si es menor, los prompts/tolerancias necesitan otra iteración antes de dar la fase por cerrada).
- [ ] Circuit breaker probado: con `ddgs` forzado a fallar, el researcher cae a `searxng` y termina la corrida.
- [ ] El runbook manual de la fase 6 sigue funcionando como fallback (probado una vez tras cerrar esta fase).

## Dependencias

Fases 4 (endpoints admin) y 6 (producción operando). Decisiones D2 (API key DeepSeek + límite) y D4 (CLI vs UI de revisión) resueltas.
