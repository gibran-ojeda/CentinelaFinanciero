# Fase 6 — Despliegue del MVP

## Objetivo

El MVP en producción con actualización **manual semanal** de tasas — exactamente la Fase conceptual F1 del foundation. Al cerrar esta fase hay un sitio público en `centinelafinanciero.lat` con TLS, sirviendo el comparador y la calculadora con el catálogo seed completo, y un runbook para mantener los datos frescos a mano mientras las fases 7–9 automatizan.

**Hosting (decisión D1):** Centinela convive con NarrativeAlpha en el **mismo VPS**, como un stack Docker **completamente independiente** (base de datos, Redis e imagen propias). El único recurso compartido es el **Caddy** del host, que ya ocupa 80/443. Sin PaaS de pago: el despliegue es íntegro en el VPS y el costo marginal del proyecto es ~$0.

---

## Contexto: lo que ya corre en ese VPS

NarrativeAlpha ocupa estos puertos — verificado en su `docker-compose.yml` y `Caddyfile`:

| Puerto | Servicio | Modo |
|---|---|---|
| 80 / 443 | `narrativealpha-caddy` | **`network_mode: host`** — único edge del VPS |
| 5432 | `narrativealpha-db` | loopback |
| 6379 | `narrativealpha-redis` | loopback |
| 8000 | `narrativealpha-api` | loopback |
| 8001 | `narrativealpha-mcp` | **todas las interfaces** |
| 8002 | `narrativealpha-dashboard` | `network_mode: host` |
| 8080 | `narrativealpha-searxng` | loopback |
| 8899 | motor Vibe-Trading (otro compose) | loopback |

Tres consecuencias de diseño, todas verificadas contra la configuración real:

1. **Centinela no levanta Caddy.** 80/443 están tomados por un Caddy en host mode. Se añade un site block al Caddyfile existente de NarrativeAlpha, que pasa a ser **infraestructura compartida del VPS**.
2. **Centinela no usa `network_mode: host`.** NarrativeAlpha lo necesita por el motor Vibe-Trading en `127.0.0.1:8899`; Centinela no tiene esa restricción. Bridge con puertos publicados en loopback basta: Caddy en host mode los alcanza por `127.0.0.1`.
3. **`ufw` dropea `docker0 → host`** en este VPS (causa documentada de 502 en NarrativeAlpha). Un contenedor de Centinela en bridge **no** alcanza nada publicado en el loopback del host. Si en el futuro Centinela quisiera consumir el SearXNG de NarrativeAlpha, la vía es adjuntar el contenedor a la red bridge de NarrativeAlpha como red externa — nunca por la gateway de Docker.

## Asignación de recursos de Centinela

| Servicio | Contenedor | Puerto (siempre `127.0.0.1`) |
|---|---|---|
| `db` | `centinela-db` | 5433 |
| `redis` | `centinela-redis` | 6380 |
| `api` | `centinela-api` | 8010 |
| `web` | `centinela-web` | 8011 |
| `scheduler` | `centinela-scheduler` | — |

`COMPOSE_PROJECT_NAME=centinela`; volúmenes `centinela-pgdata`, `centinela-redisdata`; red propia del proyecto.

## Entregables

- **Site block en el Caddyfile compartido** (en el repo de NarrativeAlpha, `docker/caddy/Caddyfile`):

  ```
  centinelafinanciero.lat {
      encode zstd gzip
      # Stack Centinela: bridge con puerto publicado en loopback.
      # 127.0.0.1 explícito (no `localhost`): puede resolver a ::1 y fallar.
      reverse_proxy 127.0.0.1:8011
  }
  ```

  La recarga se hace **sin downtime de NarrativeAlpha**: `docker exec narrativealpha-caddy caddy reload --config /etc/caddy/Caddyfile`. Documentar este acoplamiento entre repos en ambos lados (nota en el Caddyfile de NarrativeAlpha apuntando a este archivo).
- `docker-compose.yml` + `docker-compose.prod.yml` de Centinela — servicios `web`, `api`, `scheduler`, `db`, `redis` con la asignación de puertos de arriba, healthchecks, `restart: unless-stopped`, logging `json-file` rotado (10m × 3) y **límites de memoria** por servicio (el VPS ya sostiene 9 contenedores).
- **Postgres afinado para co-hosting**: `shared_buffers` y `work_mem` modestos, `max_connections` acotado — el catálogo del MVP es pequeño y la RAM se comparte con NarrativeAlpha.
- `.github/workflows/deploy.yml` — CD en push a `main`, con directorio propio en el VPS (`~/centinela-financiero`, **nunca** el de NarrativeAlpha):
  1. SSH → `git fetch && git reset --hard origin/main` (determinista).
  2. Sincronizar secretos desde GitHub Secrets al `.env`.
  3. `docker compose -p centinela build` + `up -d`.
  4. `alembic upgrade head`.
  5. **Gates duros** (abortan el deploy si fallan):
     - Migraciones aplicadas sin error.
     - Deriva de esquema: `python -m core.schema_check` deriva el contrato desde el metadata del ORM (no de una lista a mano) y lo compara contra la BD real.
     - Smoke tests HTTP: `/healthz` de la API, home del sitio (200 + contenido esperado), `GET /api/v1/meta/frescura`.
     - **Verificación de no-interferencia**: los contenedores `narrativealpha-*` siguen `healthy` y `https://narrative-alpha.cloud` responde 200 después del deploy.
  6. Recarga de Caddy solo si el site block cambió.
  7. Rollback documentado: re-deploy del commit anterior + `alembic downgrade` si la migración fue el problema.
- **Backups**: `pg_dump` diario del contenedor `centinela-db` con nombre y retención propios (14 días), sin colisionar con los de NarrativeAlpha; copia fuera del VPS; restore probado al menos una vez.
- `docs/runbook-actualizacion-manual.md` — ciclo semanal operativo:
  1. Revisar las tasas publicadas por cada institución del catálogo (URLs de `seeds/fuentes_tasas.yaml`).
  2. Actualizar `seeds/tasas.csv` con tasa, fecha del dato y URL fuente.
  3. `python -m cli tasas import` (en el VPS o en local contra prod vía túnel SSH).
  4. Verificar en el sitio que la fecha de actualización cambió.
- Monitoreo mínimo: uptime monitor externo sobre el dominio y revisión de `job_runs` incluida en el runbook.

## Tareas

1. **Antes de nada, verificar capacidad del VPS**: `free -h`, `df -h`, `docker stats`. Si la RAM libre no sostiene un Postgres más, decidir límites o ampliar el VPS — no descubrirlo a mitad del deploy.
2. Registrar el dominio `centinelafinanciero.lat` (D1b, resuelta), apuntar el DNS al VPS y activar el reenvío de `contacto@centinelafinanciero.lat` a la bandeja del operador — es el canal de corrección que publica el aviso legal.
3. Escribir el compose de producción con la asignación de puertos y los límites de memoria; levantar el stack en el VPS **sin** tocar Caddy todavía y verificar por túnel SSH que el sitio responde en `127.0.0.1:8011`.
4. Añadir el site block al Caddyfile de NarrativeAlpha, recargar Caddy y verificar que **ambos** dominios responden.
5. Escribir el workflow de CD con los gates (incluido el de no-interferencia); probar un deploy completo y un rollback simulado.
6. Programar backups y probar un restore en local desde el dump.
7. Ejecutar el primer ciclo del runbook manual completo (buscar tasas → CSV → import → verificar en sitio).
8. Configurar el uptime monitor.
9. **Apagar el modo demo**: `python -m cli config set mostrar_datos_demo false --motivo "lanzamiento público"`. Mientras esté encendido, el comparador publica las instituciones ilustrativas (◆) y las tasas en `PENDIENTE_REVISION` — marcadas, pero publicadas. Un sitio abierto a internet sólo debe servir lo verificado. Verificar después que `/api/v1/meta/frescura` devuelve `modo_demo: false`.
10. Resolver **D5** (revisión de redacción legal de banderas y disclaimers) — bloqueante para el lanzamiento público, no para tener el stack corriendo.

## Criterios de aceptación

- [ ] `https://centinelafinanciero.lat` responde con TLS válido; la API interna (8010) no es alcanzable desde internet.
- [ ] `mostrar_datos_demo` está en `false`: ninguna institución ◆ ni ninguna tasa sin verificar aparece en el sitio público.
- [ ] `https://narrative-alpha.cloud` sigue respondiendo 200 y todos los contenedores `narrativealpha-*` siguen `healthy` — cero regresión en el vecino.
- [ ] Ningún puerto de Centinela colisiona con los de NarrativeAlpha (verificar con `ss -tlnp`).
- [ ] Los volúmenes y contenedores de Centinela llevan prefijo propio; `docker volume ls` no muestra ambigüedad.
- [ ] Un push a `main` despliega automáticamente y los gates se ejecutan; un gate en rojo aborta el deploy.
- [ ] Rollback ejecutado al menos una vez como simulacro, documentado.
- [ ] Restore desde backup probado en local.
- [ ] Primer ciclo de actualización manual semanal completado de punta a punta; la fecha de frescura en el sitio lo refleja.
- [ ] Disclaimers y redacción de banderas revisados (D5 cerrada) — **antes de difundir el sitio públicamente**.

## Dependencias

Fases 4 y 5 (API + frontend funcionando en local). Acceso SSH al VPS de NarrativeAlpha y permiso para editar su Caddyfile. Decisión D5 pendiente (bloquea solo el lanzamiento público).
