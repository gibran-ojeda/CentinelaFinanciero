# Fase 7 — Ingesta Banxico (nivel 1)

## Objetivo

Automatizar todo lo gubernamental y macro con la fuente oficial: el SIE de Banxico (API REST con token gratuito). Al cerrar esta fase, CETES y las series macro (UDI, INPC, TIIE, tipo de cambio) se actualizan solos, y la TEN y la ganancia real de **todo** el comparador usan inflación viva en lugar del valor seed.

## Entregables

- `src/ingest_banxico/`:
  - `client.py` — cliente httpx async del SIE: auth por token (`Bmx-Token` header), retry/backoff exponencial sobre {429, 5xx}, rate limiting (el SIE tiene límites por token), timeout configurable.
  - `series.py` — catálogo declarativo de series: clave SIE → nombre interno → tabla destino. Series mínimas: tasas de rendimiento de CETES a 28/91/182/364 días (última subasta), valor de la UDI, INPC (índice y variación anual), TIIE 28, tipo de cambio FIX. **Las claves exactas de serie (ej. SF43936) se verifican contra el catálogo del SIE en implementación, no se asumen.**
  - `sync.py` — sincronización incremental: pide desde la última fecha almacenada por serie, upsert idempotente en `series_economicas`/`valores_serie` (unique serie+fecha).
  - `materializer.py` — convierte las series de subasta de CETES en filas de `tasas` (productos CETES del catálogo, fuente `BANXICO_API`, `fecha_dato` = fecha de subasta, estado VIGENTE directo — fuente oficial no pasa por revisión).
- Job `banxico_sync_series` en el scheduler: diario 07:00 America/Mexico_City (CronTrigger con timezone), doble gate (`SCHEDULER_BANXICO_ENABLED` env + `BANXICO_SYNC_ENABLED` en ConfigStore), lock Redis, resultado en `job_runs`.
- Cambio en el motor de consumo: la inflación para `real.py` y el valor UDI para `coverage.py` se leen de `valores_serie` (último INPC anual, última UDI) en lugar del seed; el seed queda como fallback si la serie está vacía.
- `.env.example` actualizado: `BANXICO_TOKEN`, flags del job.

## Tareas

1. Registrar el token del SIE y explorar el catálogo para confirmar las claves de cada serie; documentarlas en `series.py` con enlace al catálogo.
2. Implementar cliente + sync incremental; capturar respuestas reales del SIE como fixtures para respx.
3. Implementar el materializador de CETES → `tasas` (upsert por producto+fecha_dato: re-correr el job el mismo día no duplica).
4. Conectar INPC/UDI vivos a la calculadora y a la conversión de coberturas; verificar que `meta/frescura` refleja la fuente Banxico.
5. Registrar el job con doble gate; probar el kill-switch caliente (apagar en ConfigStore → el job siguiente no-opea sin reiniciar).
6. Tests: cliente contra fixtures respx (respuesta normal, serie vacía, 429 con retry, token inválido), sync incremental (segunda corrida no duplica), materializador idempotente, fallback a seed si la serie está vacía.

## Criterios de aceptación

- [ ] `banxico_sync_series` corre en el scheduler de producción y `job_runs` registra corridas diarias exitosas.
- [ ] Re-ejecutar el job manualmente no genera duplicados en `valores_serie` ni en `tasas`.
- [ ] La calculadora usa el INPC vivo (verificable comparando contra el valor publicado por Banxico) y los límites IPAB/PROSOFIPO en MXN se mueven con la UDI diaria.
- [ ] `GET /api/v1/meta/frescura` muestra la última sincronización de Banxico.
- [ ] El kill-switch en ConfigStore detiene el job en caliente; el flag env lo des-registra en el siguiente arranque.
- [ ] Tests respx en verde en CI sin tocar la API real.

## Dependencias

Fase 6 en producción (o fase 4 si se decide adelantarla en local — el job es independiente del frontend). Token del SIE registrado.
