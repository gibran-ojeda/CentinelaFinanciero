# Fase 8 — Ingesta CNBV y banderas (nivel 1)

## Objetivo

Alimentar el sistema de banderas con datos regulatorios reales (Fase conceptual F2): IMOR, ICAP, NICAP, cobertura de cartera vencida y captación por institución, desde los boletines mensuales del Portafolio de Información de la CNBV. Las banderas dejan de depender del seed y pasan a recomputarse con cada boletín.

## Entregables

- `src/ingest_cnbv/`:
  - `downloader.py` — descarga de los archivos del Portafolio de Información (CSV/XLSX) para banca múltiple y SOFIPOs; detección de qué periodo es el último publicado; almacenamiento del archivo crudo (trazabilidad).
  - `parser_banca.py` / `parser_sofipo.py` — parseo de los formatos de cada portafolio (son distintos entre figuras y cambian ocasionalmente entre periodos: los parsers validan encabezados esperados y fallan ruidosamente si el formato cambió, nunca ingieren basura en silencio).
  - `normalizer.py` — mapeo nombre CNBV → `instituciones.nombre_cnbv` (los nombres regulatorios difieren del nombre comercial: "Banco Nu México" vs "Nu"); reporte explícito de instituciones no mapeadas y de instituciones del catálogo sin datos en el boletín.
  - `loader.py` — upsert en `indicadores_financieros` (unique institución+periodo) con `fuente_url` del boletín.
- Job `cnbv_boletines_mensual`: CronTrigger día 5 de cada mes con **ventana de reintento** (si el boletín del periodo esperado aún no está publicado — la CNBV publica con 1–3 meses de rezago y sin fecha fija — reintenta diario hasta encontrarlo, con cooldown en Redis). Doble gate + lock + `job_runs`.
- Encadenamiento: al cargar un periodo nuevo, dispara `banderas_recompute` (fase 4) → las banderas activas cambian con el dato regulatorio real.
- Job `frescura_check` (diario): compara la última actualización de **cada** fuente (Banxico, CNBV, manual, y en fase 9 las LLM) contra su SLA configurado en ConfigStore; si se excede, registra alerta en `job_runs` y expone el estado en `meta/frescura`.
- UI (ajuste menor en frontend): los indicadores de salud y banderas muestran su periodo de referencia — "cifras a marzo 2026" — como exige §15 del foundation.

## Tareas

1. Explorar el Portafolio de Información de la CNBV; identificar los archivos exactos (banca múltiple y SOFIPOs) que contienen IMOR, ICAP, NICAP, ICOR y captación; descargar 2–3 periodos reales como fixtures.
2. Implementar downloader con detección de último periodo publicado.
3. Implementar parsers contra los fixtures reales; validación de encabezados; tests con un periodo de formato "viejo" si se consigue, para probar el fallo ruidoso.
4. Completar `nombre_cnbv` en el seed de instituciones y implementar el normalizer con reporte de no-mapeadas.
5. Implementar loader + encadenamiento con `banderas_recompute`; verificar banderas compuestas con datos históricos conocidos (ej. una SOFIPO que haya estado en N3/N4).
6. Implementar `frescura_check` con SLAs iniciales en ConfigStore (Banxico: 2 días; CNBV: 100 días; manual/LLM: 10 días).
7. Ajuste de UI del periodo de referencia.

## Criterios de aceptación

- [ ] Parsers en verde contra boletines reales descargados como fixtures (no sintéticos).
- [ ] El mapeo de nombres cubre el 100% del catálogo seed; instituciones no mapeadas generan reporte visible en `job_runs`, no fallo silencioso.
- [ ] Un cambio de formato del boletín rompe el job con error explícito — nunca carga datos malinterpretados.
- [ ] Tras cargar un periodo con IMOR > 6% para una institución, la bandera roja aparece en el comparador sin intervención manual.
- [ ] Las banderas compuestas de §5.2 se activan correctamente con un caso de datos reales o realistas.
- [ ] La UI muestra el periodo de referencia de todo indicador de salud.
- [ ] `frescura_check` alerta cuando una fuente excede su SLA (probar bajando el SLA en caliente).

## Dependencias

Fases 2–3 (esquema y motor de banderas); fase 4 (`banderas_recompute`). Independiente de la fase 7 — pueden ejecutarse en paralelo.
