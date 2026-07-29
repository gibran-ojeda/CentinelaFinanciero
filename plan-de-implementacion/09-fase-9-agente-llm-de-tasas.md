# Fase 9 — Agente LLM de tasas (niveles 2 y 3)

## Objetivo

Automatizar las tasas que **no tienen API** (SOFIPOs, neobancos, PRLV, BONDDIA) con la estrategia de tres niveles de §15 del foundation: fetch dirigido + extracción LLM como mecanismo primario, búsqueda abierta con agente LLM solo para descubrimiento y verificación, y cola de revisión humana como control de calidad. El runbook manual de la fase 6 queda como fallback documentado — no se elimina.

> **Contexto de la decisión** (evaluada en la planificación): la API de DeepSeek no tiene búsqueda web nativa; el patrón adoptado es *tool-use* con ejecutor de búsqueda propio, heredado de NarrativeAlpha. El scraping clásico por selectores CSS se descartó: 20+ scrapers por institución se rompen en silencio con cada rediseño. El LLM extrae de la página lo que un selector no sobreviviría.

## Entregables

### Infraestructura LLM — `src/llm/`
- `providers/base.py` — ABC `BaseLLMProvider` + jerarquía de errores (`LLMProviderError`, `LLMRateLimitError`, `LLMTimeoutError`, `LLMParseError`) + dataclass `LLMResponse` (content, tool_calls, finish_reason, tokens, cost).
- `providers/openai_compat.py` — un provider cubre DeepSeek/OpenAI/compatibles vía `AsyncOpenAI` con `base_url` intercambiable; tabla de pricing por modelo y cálculo de costo por llamada.
- `client.py` — router por tiers (`extraction` → modelo económico p.ej. `deepseek-chat`; `research` → el mismo u otro según calibración) con fallback y retry. **Nota heredada de NarrativeAlpha: no usar modelos razonadores en el tool-loop** (no emiten JSON confiable con tools activos).
- `cost_tracker.py` — acumulado diario en Redis con límite duro **`LLM_COST_DAILY_LIMIT_USD=1.0`** (decisión D2 resuelta: $1 USD/día): al alcanzarlo, los jobs LLM no-opean y lo registran en `job_runs`. Con el volumen esperado (~25 páginas semanales y un modelo económico) el gasto real debería quedar uno o dos órdenes de magnitud por debajo del techo; el límite es una red de seguridad contra bucles, no un presupuesto operativo.
- `parsers.py` — `clean_llm_json` / `safe_parse_json(content, required_keys)`: quita fences markdown y bloques `<think>`, extrae el objeto JSON correcto.
- `prompts/` (carpeta raíz del repo) — plantillas `.md` externas cargadas con cache y renderizadas con `format_map`: `extract_rates_system.md`, `extract_rates_user.md`, `research_system.md`, `research_user.md`.

### Agente de tasas — `src/rates_agent/`
- `fetcher.py` — **nivel 2, parte determinista. Hecho.** Recorre `fuentes_tasas` activas con las cuatro capas de resiliencia portadas de NarrativeAlpha (reintento con backoff y jitter → cadena `httpx → navegador` → circuit breaker por host y corrida → backoff temporal con reset half-open), distinguiendo **vacío de error duro**. `trafilatura` extrae el texto; el hash del contenido evita llamar al LLM cuando la página no cambió.

  > **El bot se identifica y no evade.** User-Agent propio con URL de contacto y `robots.txt` respetado. Si una institución bloquea a un bot identificado, esa fuente pasa a lectura manual y se registra por qué — se cambió el «headers de navegador» que decía antes este documento, que era impersonación.

  > **Navegador: aplazado en el VPS.** `TransporteNavegador` existe y está probado, pero Chromium no va en la imagen: ~450 MB de disco y ~300 MB de RAM contra un límite de 256 MB en el scheduler. El job del VPS corre `tasas_fetch_solo_sin_js=true` y las ocho fuentes con JavaScript se leen desde local con `cli tasas fetch --solo-navegador`. La decisión, su costo y qué la reabre están en [docs/despliegue.md](../docs/despliegue.md#navegador-en-el-vps--decisión-aplazada).
- `extractor.py` — **nivel 2, parte LLM**: contenido de página → DeepSeek con prompt de extracción → JSON validado contra esquema pydantic `TasaExtraida {producto, tipo, plazo_dias, tasa_nominal, gat_nominal?, gat_real?, monto_minimo?, condiciones?, confianza}`. Rechaza respuestas que no validen; reintento con feedback del error de validación (1 vez).
- `researcher.py` — **nivel 3**: tool-use loop con tool `web_search`; `SearchExecutor` determinista con **`ddgs` como único backend inicial** (librería, $0, sin API keys ni infraestructura), con retry → cadena de fallbacks entre engines (`ddgs` → `ddgs:google` → `ddgs:brave`) → circuit breaker por corrida; **invariante anti-alucinación**: se acumulan las URLs devueltas por búsquedas reales (`allowed_urls`) y todo hallazgo cuyas URLs no estén en ese conjunto se descarta; tras N rondas se retiran los tools para forzar la respuesta JSON final. Uso: instituciones con fetch fallido, fuentes stale, y descubrimiento (¿nueva SOFIPO? ¿cambió la URL de tasas?).

  > **Sobre SearXNG:** Centinela **no levanta un SearXNG propio** (~250 MB de RAM en un VPS ya compartido con NarrativeAlpha, ver fase 06). En NarrativeAlpha SearXNG existe porque el backtest hace búsqueda intensiva; aquí el nivel 3 es un camino ocasional de descubrimiento, no el camino caliente. Si la calibración muestra que `ddgs` no basta, la vía es **reutilizar el SearXNG que ya corre en el VPS** adjuntando el `scheduler` de Centinela a la red bridge de NarrativeAlpha como red externa y apuntando a `http://searxng:8080` — nunca por la gateway de Docker, que `ufw` bloquea en ese VPS. El `SearchExecutor` se diseña con el backend intercambiable por configuración para que ese cambio sea una variable de entorno, no una refactorización.
- `reviewer.py` — control de calidad: compara cada `TasaExtraida` contra la tasa vigente del producto. Dentro de tolerancia (ConfigStore, p.ej. ±0.5pp) → publica directo como VIGENTE con `fuente_url` y fuente `FETCH_DIRIGIDO`/`LLM_RESEARCH`. Fuera de tolerancia, producto nuevo, o confianza baja → fila en estado `PENDIENTE_REVISION` + entrada en `revisiones_tasas`; **nunca se publica sola**.
- Flujo de aprobación (decisión D4 resuelta — **CLI al inicio**): endpoints admin de la fase 4 (`GET/POST /admin/revisiones`) + `python -m cli revisiones list|approve|reject`, con salida legible (diff tasa anterior → nueva, institución, producto, URL fuente) para resolver la cola en minutos. Si tras la calibración la cola supera ~20 entradas semanales de forma sostenida, se reevalúa construir la mini-UI admin.

### Jobs y servicios
- `tasas_fetch_dirigido` — lunes 06:00 CDMX: fetcher + extractor + reviewer sobre todas las `fuentes_tasas` activas.
- `tasas_research_abierta` — miércoles 06:00 CDMX: researcher **solo** sobre instituciones stale (sin dato fresco según SLA) o con fetch fallido dos veces.
- Ambos jobs con doble gate, lock Redis, `job_runs` con métricas (páginas procesadas, tokens, costo USD, tasas publicadas/en cola).

## Tareas

1. Implementar `src/llm/` (base → openai_compat → client → cost_tracker → parsers) con tests respx contra la forma de respuesta de la API de DeepSeek.
2. Escribir los prompts de extracción; calibrar contra 5–10 páginas reales del catálogo (guardadas como fixtures) hasta que la extracción sea estable.
3. Implementar fetcher con hash de contenido; catalogar en `fuentes_tasas` qué instituciones requieren JS.
4. Implementar extractor + reviewer + flujo de aprobación CLI.
5. Implementar SearchExecutor (backend intercambiable por configuración, `ddgs` inicial) y el tool-loop del researcher con la invariante `allowed_urls`; prompt con reglas duras (solo URLs de resultados reales, ventana de fechas, JSON final obligatorio, "sin datos" es respuesta válida).
6. Registrar ambos jobs con doble gate y su lock Redis.
7. Corrida de calibración: 2–3 semanas midiendo tasa de aprobación de la cola, costo semanal y falsos (tasas mal extraídas que pasaron tolerancia); ajustar prompts y tolerancias en ConfigStore. Si `ddgs` resulta insuficiente, evaluar la reutilización del SearXNG del VPS descrita arriba.

## Criterios de aceptación

- [ ] **Ninguna** tasa con fuente `LLM_RESEARCH` o `FETCH_DIRIGIDO` existe en estado VIGENTE sin `fuente_url` poblada; para research, la URL proviene verificablemente de `allowed_urls` (test de la invariante).
- [ ] Una extracción fuera de tolerancia termina en `revisiones_tasas` y NO cambia la tasa vigente hasta aprobarse.
- [ ] Si el contenido de una página no cambió (hash), el job no gasta tokens en ella.
- [ ] El CostTracker corta los jobs al llegar al límite de $1 USD/día y lo registra; el costo semanal medido queda muy por debajo (esperado: centavos de USD).
- [ ] Tasa de aprobación de la cola ≥ 80% tras la calibración (si es menor, los prompts/tolerancias necesitan otra iteración antes de dar la fase por cerrada).
- [ ] Circuit breaker probado: con el engine primario forzado a fallar, el researcher cae al siguiente de la cadena y termina la corrida; si todos fallan, la corrida se marca DEGRADADA en `job_runs` sin publicar nada.
- [ ] La cola de revisión se resuelve por CLI en minutos; se mide su volumen semanal para decidir sobre la mini-UI (D4).
- [ ] El runbook manual de la fase 6 sigue funcionando como fallback (probado una vez tras cerrar esta fase).

## Dependencias

Fases 4 (endpoints admin) y 6 (producción operando). Requiere la API key de DeepSeek en el `.env` de producción (límite ya fijado en $1 USD/día por D2).
