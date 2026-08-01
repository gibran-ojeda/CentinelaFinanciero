# Despliegue

> Centinela vive en el **mismo VPS que NarrativeAlpha**, como stack Docker independiente. Lo único compartido es el Caddy del host. Todo lo de aquí asume esa restricción; el porqué está en [§14 del foundation](../foundation-comparador-financiero-mx.md) y en la [fase 06 del plan](../plan-de-implementacion/06-fase-6-despliegue-mvp.md).

## Lo que ocupa Centinela

| Servicio | Contenedor | Puerto (siempre `127.0.0.1`) |
|---|---|---|
| `db` | `centinela-db` | 5433 |
| `redis` | `centinela-redis` | 6380 |
| `api` | `centinela-api` | 8010 |
| `web` | `centinela-web` | **8011** ← el que apunta Caddy |
| `scheduler` | `centinela-scheduler-1` | — |

Nada se publica fuera del loopback. A internet sólo llega lo que Caddy sirve.

---

## Antes del primer despliegue

**1. Comprobar que la máquina aguanta.** Antes de nada, no a mitad del deploy:

```bash
free -h && df -h / && docker stats --no-stream
```

El overlay de producción reserva ~1.5 GB de techo entre los cinco servicios (db 512M · api 384M · web 256M · scheduler 256M · redis 128M). El uso real medido en local es de ~175 MB. Si la RAM libre no da, se bajan los límites en `docker-compose.prod.yml` antes de seguir.

**2. Dominio.** `centinelafinanciero.lat` (D1b), con un registro A al VPS. Activar también el reenvío de `contacto@centinelafinanciero.lat` — es el canal que publica el [aviso legal](../frontend/src/pages/aviso-legal.astro) y tiene que existir el día que el sitio sea público.

**3. Directorio propio en el VPS**, nunca el de NarrativeAlpha:

```bash
git clone https://github.com/gibran-ojeda/brujula-financiera.git ~/centinela-financiero
```

> El repositorio conserva el nombre viejo; todo lo demás —contenedores, volúmenes, base, usuario, directorio— dice `centinela`.

**4. Secretos en GitHub** (Settings → Secrets → Actions):

| Secreto | Qué es |
|---|---|
| `VPS_HOST`, `VPS_USER` | destino del SSH |
| `VPS_SSH_KEY` | clave privada del despliegue, sin passphrase |
| `VPS_HOST_KEY` | salida de `ssh-keyscan <host>`. Se fija a propósito: sin ella habría que aceptar la huella a ciegas, y eso convierte un secuestro de DNS en una sesión con nuestras credenciales dentro |
| `POSTGRES_PASSWORD` | contraseña de la base |
| `API_READ_KEY`, `API_ADMIN_KEY` | `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `SITE_URL` | `https://centinelafinanciero.lat` |
| `BANXICO_TOKEN` | token del SIE ([solicitud](https://www.banxico.org.mx/SieAPIRest/service/v1/token)). Opcional: sin él, `banxico_sync_series` se marca OMITIDO y la UDI cae al valor de respaldo congelado |
| `DEEPSEEK_API_KEY` | llave de DeepSeek. Opcional: sin ella, el fetch L2 de los lunes falla (FALLIDO en `job_runs`, no en silencio) |

También existe la **variable** de repo `SCHEDULER_RESEARCH_ENABLED` (no
secreto): el gate frío del researcher L3. Sin definirla queda apagado.

> **Ojo**: el compose no declara `env_file`, así que una variable sólo llega al
> contenedor si está en el mapa `environment:` del servicio. Añadir una nueva
> exige tocar tres sitios: el secreto/variable en GitHub, el heredoc de
> `scripts/desplegar.sh` y el mapa del compose.

**5. Levantar el stack sin tocar Caddy**, y comprobar por túnel:

```bash
ssh -L 8011:127.0.0.1:8011 <usuario>@<vps>
```

Con el túnel abierto, `http://127.0.0.1:8011` desde el navegador local.

**6. El site block en el Caddyfile de NarrativeAlpha** (`docker/caddy/Caddyfile` de *ese* repo):

```caddyfile
# Stack Centinela — repo aparte, ver docs/despliegue.md de centinela.
# 127.0.0.1 explícito, no `localhost`: puede resolver a ::1 y fallar.
centinelafinanciero.lat {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8011
}
```

Recarga sin downtime del vecino:

```bash
docker exec narrativealpha-caddy caddy reload --config /etc/caddy/Caddyfile
```

Dejar una nota en ese Caddyfile apuntando aquí: el acoplamiento entre los dos repos tiene que estar escrito en los dos lados.

---

## Despliegue

Automático en push a `main`. El [workflow](../.github/workflows/deploy.yml) no lleva la lógica dentro: manda [`scripts/desplegar.sh`](../scripts/desplegar.sh) y [`scripts/gates.sh`](../scripts/gates.sh) por stdin. Los secretos van por stdin y no como argumentos de `ssh` porque `ssh host VAR=x bash` los deja en la línea de comandos del proceso remoto, a la vista de cualquier `ps`.

Qué hace, en orden:

1. `git fetch` + `reset --hard` al commit. No `pull`: el estado del VPS lo fija el commit, no lo que alguien dejara ahí arreglando algo.
2. Regenera `.env` desde los secretos. Es la única copia y nadie la edita en la máquina.
3. `build` + `up -d` con el overlay de producción.
4. Espera a que la API esté `healthy` (o vuelca sus logs y aborta).
5. `alembic upgrade head`.
6. **Gates.** Cualquiera en rojo aborta:

| Gate | Qué comprueba |
|---|---|
| Deriva de esquema | `python -m core.schema_check`: la base está en el head **y** coincide con el ORM. Puede estar en el head y aun así diferir |
| Humo HTTP | `/healthz`, `/meta/frescura`, y la portada **con filas** — un 200 con la tabla vacía es justo lo que hay que ver — y que la canónica apunte al dominio y no al loopback |
| No interferencia | Los `narrativealpha-*` siguen `healthy` y su dominio responde 200 |
| TLS público | `https://centinelafinanciero.lat` responde 200 |

Los gates corren desde el repo que el despliegue acaba de actualizar, así que las comprobaciones son siempre las del commit que se está verificando.

### Rollback

Actions → **deploy** → *Run workflow*, y en `ref` el SHA anterior. Si el problema fue la migración, bajarla antes a mano:

```bash
ssh <vps> 'cd ~/centinela-financiero && docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T api alembic downgrade -1'
```

---

## Respaldos

Cron diario en el VPS:

```cron
15 3 * * * /home/<usuario>/centinela-financiero/scripts/respaldar.sh >> /home/<usuario>/centinela-financiero/backups/respaldo.log 2>&1
```

Retención de 14 días, nombre con prefijo propio para no pisarse con los de NarrativeAlpha. El volcado se escribe a un temporal y sólo se renombra al terminar: un corte a la mitad no deja un `.sql.gz` truncado con pinta de respaldo bueno. La rotación va **después** del volcado, nunca antes.

**Copia fuera del VPS** — semanal, desde la máquina local, junto con el ciclo del [runbook](runbook-actualizacion-manual.md):

```bash
scp <usuario>@<vps>:~/centinela-financiero/backups/centinela-*.sql.gz ~/respaldos-centinela/
```

**Probar la restauración.** Un respaldo que nadie restauró es un archivo, no un respaldo. Por defecto se restaura en un Postgres desechable y se comprueba que los datos están:

```bash
scripts/restaurar.sh ~/respaldos-centinela/centinela-20260728-031500.sql.gz
```

Para escribir sobre la base real hace falta pedirlo: `CONTENEDOR=centinela-db CONFIRMO_SOBRESCRIBIR=si`.

---

## Monitoreo

- **Uptime externo** (UptimeRobot gratuito basta) sobre `https://centinelafinanciero.lat/`, cada 5 minutos.
- **`job_runs`** se revisa en el ciclo semanal del runbook: es donde el scheduler deja constancia de cada corrida.

---

## Navegador en el VPS — decisión aplazada

**Estado: aplazada, no descartada.** Fecha: 2026-07-29.

Once de las dieciocho fuentes de tasas se pintan con JavaScript y sólo rinden a un navegador. `TransporteNavegador` está escrito y probado, y la cadena del [fetcher](../src/rates_agent/fetcher.py) lo acepta como segundo eslabón sin tocar nada — pero **no se instala en la imagen**, y el job del VPS corre sólo las de nivel 2 que rinden a un cliente HTTP plano — tres: las dos de cetesdirecto y la de Supertasas (`tasas_fetch_solo_sin_js=true` en el ConfigStore; las cuatro portadas de nivel 3 sin JS alimentan al researcher, no al extractor).

**Por qué no.** Dos costos, y uno es bloqueante:

| | Costo |
|---|---|
| Disco | ~450 MB en la **imagen única**, que también sirve la API. §13 del foundation es «build una vez, deploy N», así que el peso lo paga cada servicio |
| RAM | Chromium usa ~300 MB al cargar una página. El límite del scheduler en [docker-compose.prod.yml](../docker-compose.prod.yml) es **256 MB**: tal cual, el OOM killer lo mata a media corrida |

Lo bloqueante es la RAM. Subirlo a ~768 MB en un VPS que ya sostiene nueve contenedores de NarrativeAlpha no es un compromiso que se tome sin medir, y esa medición es la tarea 1 de la fase 06.

**Qué se hace mientras.** El mismo código, desde la máquina local, como paso del [ciclo semanal](runbook-actualizacion-manual.md):

```bash
python -m cli tasas fetch --solo-navegador
```

El resultado entra por la misma cola de revisión. La diferencia con la opción completa es **quién dispara la corrida**, no qué hace ni cómo se aprueba.

**Qué la reabre.** Cualquiera de estas tres:

1. `free -h` muestra ≥ 1 GB en la columna **available** de forma sostenida tras el despliegue (`available`, no `free`: `free` no cuenta el page cache recuperable y subestima lo que de verdad hay).
2. El ciclo semanal se salta la pasada local dos semanas seguidas — señal de que el paso manual no se sostiene.
3. Las fuentes que necesitan navegador pasan de once a más de la mitad del catálogo.

**Qué costaría hacerlo.** Poco código: `playwright install chromium` en [docker/app/Dockerfile](../docker/app/Dockerfile), subir el límite del scheduler en el overlay, poner `tasas_fetch_solo_sin_js=false` en el ConfigStore — y decidir si el peso extra justifica separar la imagen única en dos.

---

## Cosas que muerden en este VPS

- **`ufw` dropea `docker0 → host`.** Un contenedor de Centinela en bridge no alcanza nada publicado en el loopback del host — causa documentada de 502 en NarrativeAlpha. Si algún día Centinela necesita consumir un servicio del vecino (su SearXNG, por ejemplo), la vía es adjuntar el contenedor a la red bridge de NarrativeAlpha como red externa, nunca pasar por la gateway de Docker.
- **Centinela no levanta Caddy.** 80/443 los tiene el de NarrativeAlpha en `network_mode: host`.
- **`SITE_URL` es obligatoria en producción.** El overlay falla si no está, a propósito: sin ella la canónica y el `sitemap.xml` anunciarían el loopback y el fallo sería invisible hasta que el índice estuviera hecho.
- **La política de transición del lanzamiento** publica las tasas en
  `PENDIENTE_REVISION` etiquetadas «sin verificar» hasta que su lectura oficial
  las sustituye (`mostrar_tasas_sin_verificar`, encendida por diseño). Para
  ocultar lo no verificado sin deploy:

  ```bash
  docker compose exec -T api python -m cli config set mostrar_tasas_sin_verificar false --motivo "solo verificado"
  ```

  El estado es observable en `/api/v1/meta/frescura`
  (`mostrar_tasas_sin_verificar`).
