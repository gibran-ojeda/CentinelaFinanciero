# Fase 6 — Despliegue del MVP

## Objetivo

El MVP en producción con actualización **manual semanal** de tasas — exactamente la Fase conceptual F1 del foundation. Al cerrar esta fase hay un sitio público en `centinelafinanciero.lat` con TLS, sirviendo el comparador y la calculadora con el catálogo seed completo, y un runbook para mantener los datos frescos a mano mientras las fases 7–9 automatizan.

**Hosting (decisión D1):** Centinela convive con NarrativeAlpha en el **mismo VPS**, como un stack Docker **completamente independiente** (base de datos, Redis e imagen propias). El único recurso compartido es el **Caddy** del host, que ya ocupa 80/443. Sin PaaS de pago: el despliegue es íntegro en el VPS y el costo marginal del proyecto es ~$0.

---

## Contexto: lo que ya corre en ese VPS

El VPS ya sostiene otro stack (NarrativeAlpha), verificado contra su configuración real: su Caddy corre en `network_mode: host` como **único edge de la máquina**, ocupando 80/443, y varios servicios suyos ocupan puertos publicados en loopback — de ahí que Centinela use 5433/6380/8010/8011 y no los estándar.

Tres consecuencias de diseño:

1. **Centinela no levanta Caddy.** 80/443 están tomados por un Caddy en host mode. Se añade un site block al Caddyfile existente del vecino, que pasa a ser **infraestructura compartida del VPS**.
2. **Centinela no usa `network_mode: host`.** El vecino lo necesita por un servicio suyo anclado al loopback del host; Centinela no tiene esa restricción. Bridge con puertos publicados en loopback basta: Caddy en host mode los alcanza por `127.0.0.1`.
3. **`ufw` dropea `docker0 → host`** en este VPS. Un contenedor de Centinela en bridge **no** alcanza nada publicado en el loopback del host. Si en el futuro Centinela quisiera consumir un servicio del vecino, la vía es adjuntar el contenedor a la red bridge del vecino como red externa — nunca por la gateway de Docker.

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

- **Site block en el Caddyfile compartido** (vive en el repo del stack vecino):

  ```
  centinelafinanciero.lat {
      encode zstd gzip
      # Stack Centinela: bridge con puerto publicado en loopback.
      # 127.0.0.1 explícito (no `localhost`): puede resolver a ::1 y fallar.
      reverse_proxy 127.0.0.1:8011
  }
  ```

  La recarga se hace **sin downtime del vecino**: `docker exec <contenedor-caddy> caddy reload --config /etc/caddy/Caddyfile`. Documentar este acoplamiento entre repos en ambos lados (nota en ese Caddyfile apuntando a este archivo).
- [`docs/despliegue.md`](../docs/despliegue.md) — prerrequisitos, secretos, site block de Caddy, rollback, respaldos y las trampas de este VPS.
- `docker-compose.yml` + [`docker-compose.prod.yml`](../docker-compose.prod.yml) de Centinela — servicios `web`, `api`, `scheduler`, `db`, `redis` con la asignación de puertos de arriba, healthchecks, `restart: unless-stopped`, logging `json-file` rotado (10m × 3) y **límites de memoria** por servicio (el VPS ya sostiene 9 contenedores).
- **Postgres afinado para co-hosting**: `shared_buffers` y `work_mem` modestos, `max_connections` acotado — el catálogo del MVP es pequeño y la RAM se comparte con NarrativeAlpha.
- [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml) — CD en push a `main`, con directorio propio en el VPS (`~/centinela-financiero`, **nunca** el de NarrativeAlpha). La lógica vive en [`scripts/desplegar.sh`](../scripts/desplegar.sh) y [`scripts/gates.sh`](../scripts/gates.sh) y viaja por stdin, no en el YAML: así se lee y se corre fuera de un workflow, y los secretos no acaban en la línea de comandos del proceso remoto.
  1. `git fetch && git reset --hard <ref>` (determinista).
  2. Regenerar `.env` desde GitHub Secrets.
  3. `build` + `up -d` con el overlay de producción.
  4. Esperar a que la API esté `healthy`; si no llega, volcar sus logs y abortar.
  5. `alembic upgrade head`.
  6. **Gates duros** (abortan el deploy si fallan):
     - Deriva de esquema: `python -m core.schema_check` deriva el contrato desde el metadata del ORM (no de una lista a mano) y lo compara contra la BD real, además de comprobar que esté en el head.
     - Humo HTTP: `/healthz`, `GET /api/v1/meta/frescura`, y la portada **con filas** — un 200 con la tabla vacía es justo el fallo que hay que ver — más que la canónica apunte al dominio y no al loopback.
     - **No-interferencia**: los contenedores del vecino siguen `healthy` y su dominio responde 200 después del deploy (señas por variables de repo, ver docs/despliegue.md).
     - TLS público: `https://centinelafinanciero.lat` responde 200.
  7. Rollback: `workflow_dispatch` con el SHA anterior en `ref`, más `alembic downgrade -1` a mano si la migración fue el problema.
- **Respaldos**: [`scripts/respaldar.sh`](../scripts/respaldar.sh) — `pg_dump` diario del contenedor `centinela-db` con nombre y retención propios (14 días), sin colisionar con los de NarrativeAlpha; copia fuera del VPS por `scp` en el ciclo semanal; [`scripts/restaurar.sh`](../scripts/restaurar.sh) restaura en un Postgres desechable y comprueba que los datos estén.
- [`docs/runbook-actualizacion-manual.md`](../docs/runbook-actualizacion-manual.md) — ciclo semanal operativo, apoyado en `python -m cli tasas pendientes`:
  1. `cli tasas pendientes` lista lo que no puede salir al sitio, con la URL oficial de cada institución.
  2. Abrir cada página —once necesitan navegador de verdad— y anotar tasa, plazo, GAT y fecha.
  3. Actualizar `seeds/tasas.csv` y correr `python -m cli tasas import`.
  4. Verificar en el sitio que la fecha cambió y que el enlace a la fuente lleva a donde debe.
- Monitoreo mínimo: uptime monitor externo sobre el dominio y revisión de `job_runs` incluida en el runbook.

  > **Nota (2026-08-01):** el ciclo manual quedó relevado por las ingestas de las fases 7-9 y por Chromium en la imagen del VPS (el job del lunes lee las dieciocho fuentes). El runbook sobrevive como procedimiento de respaldo y su paso de navegador desde laptop desapareció.

## Tareas

1. **Antes de nada, verificar capacidad del VPS**: `free -h`, `df -h`, `docker stats`. Si la RAM libre no sostiene un Postgres más, decidir límites o ampliar el VPS — no descubrirlo a mitad del deploy. *(Nota 2026-08-01: el resultado nunca se registró; el registro vive ahora en la sección «Navegador en el VPS» de docs/despliegue.md, donde es condición del límite de 768M del scheduler.)*
2. Registrar el dominio `centinelafinanciero.lat` (D1b, resuelta), apuntar el DNS al VPS y activar el reenvío de `contacto@centinelafinanciero.lat` a la bandeja del operador — es el canal de corrección que publica el aviso legal.
3. Escribir el compose de producción con la asignación de puertos y los límites de memoria; levantar el stack en el VPS **sin** tocar Caddy todavía y verificar por túnel SSH que el sitio responde en `127.0.0.1:8011`.
4. Añadir el site block al Caddyfile de NarrativeAlpha, recargar Caddy y verificar que **ambos** dominios responden.
5. Escribir el workflow de CD con los gates (incluido el de no-interferencia); probar un deploy completo y un rollback simulado.
6. Programar backups y probar un restore en local desde el dump.
7. **Antes de apuntar el DNS**, ejecutar el primer ciclo del runbook manual completo (revisar las URLs oficiales → CSV → import → verificar en sitio). Va **antes** del lanzamiento y no después: con el modo demo apagado, el catálogo verificado son cinco filas de CETES y BONDDIA, y un comparador de SOFIPOs y bancos que no muestra ninguna de las dos cosas no sirve. Lo que no se logre verificar contra la página de la propia institución se queda en `PENDIENTE_REVISION` y lo recupera la fase 9.
8. Configurar el uptime monitor.
9. **Apagar el modo demo**: `python -m cli config set mostrar_datos_demo false --motivo "lanzamiento público"`. Mientras esté encendido, el comparador publica las instituciones ilustrativas (◆) y las tasas en `PENDIENTE_REVISION` — marcadas, pero publicadas. Un sitio abierto a internet sólo debe servir lo verificado. Verificar después que `/api/v1/meta/frescura` devuelve `modo_demo: false`.

   > **Nota (2026-07-31):** superado por la política de transición del lanzamiento. Las instituciones ilustrativas se **purgaron** del producto (seeds y base), y la bandera pasó a llamarse `mostrar_tasas_sin_verificar` y **queda encendida**: las tasas de agregador se publican etiquetadas «sin verificar» hasta que la lectura oficial de cada producto las sustituye. El interruptor se conserva para ocultar lo no verificado sin deploy. Ver `docs/criterios-de-redaccion.md` §3.
10. **D5 está resuelta** (ver [criterios de redacción](../docs/criterios-de-redaccion.md)). Lo que queda de ella en esta fase es publicar `/aviso-legal` y `/privacidad` con el `contacto@` ya operativo, y releer los cinco criterios antes de difundir el sitio.

## Criterios de aceptación

- [ ] `https://centinelafinanciero.lat` responde con TLS válido; la API interna (8010) no es alcanzable desde internet.
- [ ] ~~`mostrar_datos_demo` está en `false`: ninguna institución ◆ ni ninguna tasa sin verificar aparece en el sitio público.~~ **Superado (2026-07-31)**: ver la nota del paso 9 — las ficticias se purgaron y lo pendiente se publica etiquetado bajo `mostrar_tasas_sin_verificar`.
- [ ] El dominio del vecino sigue respondiendo 200 y todos sus contenedores siguen `healthy` — cero regresión en el vecino.
- [ ] Ningún puerto de Centinela colisiona con los de NarrativeAlpha (verificar con `ss -tlnp`).
- [ ] Los volúmenes y contenedores de Centinela llevan prefijo propio; `docker volume ls` no muestra ambigüedad.
- [ ] Un push a `main` despliega automáticamente y los gates se ejecutan; un gate en rojo aborta el deploy.
- [ ] Rollback ejecutado al menos una vez como simulacro, documentado.
- [ ] Restore desde backup probado en local.
- [ ] Primer ciclo de actualización manual semanal completado de punta a punta **antes del lanzamiento**; la fecha de frescura en el sitio lo refleja.
- [ ] `/aviso-legal` y `/privacidad` publicadas y enlazadas desde el pie; `contacto@centinelafinanciero.lat` recibe correo.
- [ ] Cada tasa del sitio enlaza a la página de la que se leyó.

## Dependencias

Fases 4 y 5 (API + frontend funcionando en local). Acceso SSH al VPS de NarrativeAlpha y permiso para editar su Caddyfile. D1b y D5 resueltas.
