# Fase 6 — Despliegue del MVP

## Objetivo

El MVP en producción con actualización **manual semanal** de tasas — exactamente la Fase conceptual F1 del foundation. Al cerrar esta fase hay un sitio público, con dominio y TLS, sirviendo el comparador y la calculadora con el catálogo seed, y un runbook para mantener los datos frescos a mano mientras las fases 7–9 automatizan.

## Entregables

- `docker/caddy/Caddyfile` — edge TLS automático para el dominio (decisión D1 del overview: dominio y VPS a resolver antes de empezar esta fase); todo el tráfico rutea a `web`; la API interna no se expone.
- `docker-compose.prod.yml` (o overrides) — servicios `caddy`, `web`, `api`, `scheduler`, `db`, `redis` con puertos internos en loopback, volúmenes nombrados, healthchecks y logging rotado.
- `.github/workflows/deploy.yml` — CD en push a `main`: SSH al VPS → `git fetch && git reset --hard origin/main` (determinista) → sincronización de secretos desde GitHub Secrets al `.env` → build → **gates duros** antes de dar por bueno el deploy:
  1. `alembic upgrade head` aplicado sin error.
  2. Verificación de deriva de esquema: script `python -m core.schema_check` que deriva el contrato desde el metadata del ORM (no de una lista a mano) y lo compara contra la BD real.
  3. Smoke tests HTTP: `/healthz` de la API, home del sitio (200 + contenido esperado), `GET /api/v1/meta/frescura` vía red interna.
  4. Rollback documentado: re-deploy del commit anterior + `alembic downgrade` si la migración fue el problema.
- **Backups**: `pg_dump` diario programado (cron del VPS o job del scheduler) con retención de 14 días y copia fuera del VPS; runbook de restore probado al menos una vez.
- `docs/runbook-actualizacion-manual.md` — el ciclo semanal operativo:
  1. Revisar las tasas publicadas por cada institución del catálogo (lista de URLs de `seeds/fuentes_tasas.yaml`).
  2. Actualizar `seeds/tasas.csv` con fecha del dato y URL fuente.
  3. `python -m cli tasas import` (local contra prod vía túnel, o en el VPS).
  4. Verificar en el sitio que la fecha de actualización cambió.
- Monitoreo mínimo: alerta si `/healthz` falla (uptime monitor externo gratuito) y revisión de `job_runs` en el runbook.

## Tareas

1. Resolver decisión D1 (dominio + VPS) y D5 (revisión de redacción legal de banderas y disclaimers — **bloqueante para el lanzamiento público**).
2. Escribir Caddyfile y compose de producción; probar el stack completo en el VPS con dominio real y TLS.
3. Escribir el workflow de CD con los gates; probar un deploy completo y un rollback simulado.
4. Programar backups y probar un restore en local desde el dump.
5. Ejecutar el primer ciclo del runbook manual completo (buscar tasas → CSV → import → verificar en sitio).
6. Configurar el uptime monitor.

## Criterios de aceptación

- [ ] El sitio responde en el dominio con TLS válido; la API interna no es alcanzable desde internet.
- [ ] Un push a `main` despliega automáticamente y los 3 gates (migraciones, deriva de esquema, smoke) se ejecutan; un gate en rojo aborta el deploy.
- [ ] Rollback ejecutado al menos una vez como simulacro, documentado.
- [ ] Restore desde backup probado en local.
- [ ] Primer ciclo de actualización manual semanal completado de punta a punta; la fecha de frescura en el sitio lo refleja.
- [ ] Disclaimers y redacción de banderas revisados (D5 cerrada).

## Dependencias

Fases 4 y 5 (API + frontend funcionando en local). Decisiones D1 y D5 resueltas.
