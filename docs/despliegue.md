# Despliegue

> Centinela vive en el **mismo VPS que el stack vecino (NA)**, como stack Docker independiente. Lo único compartido es el Caddy del host. Todo lo de aquí asume esa restricción; el porqué está en [§14 del foundation](../foundation-comparador-financiero-mx.md), y este documento es la referencia operativa completa.

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

El overlay de producción reserva ~2.0 GB de techo entre los cinco servicios (db 512M · api 384M · web 256M · scheduler **768M** · redis 128M) — el scheduler sube por Chromium, y su condición es la medición registrada en «Navegador en el VPS», más abajo. Si la RAM disponible no da, el repliegue inmediato es `cli config set tasas_fetch_solo_sin_js true` y bajar el límite en `docker-compose.prod.yml`.

**2. Dominio.** `centinelafinanciero.lat` (D1b), con un registro A al VPS. Activar también el reenvío de `contacto@centinelafinanciero.lat` — es el canal que publica el [aviso legal](../frontend/src/pages/aviso-legal.astro) y tiene que existir el día que el sitio sea público.

**3. Directorio propio en el VPS**, nunca el de NA:

```bash
git clone https://github.com/gibran-ojeda/centinela-financiero.git ~/centinela-financiero
```

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
| `DEEPSEEK_API_KEY` | llave de DeepSeek. Opcional: sin ella, el fetch L2 (cada 4 horas) falla (FALLIDO en `job_runs`, no en silencio). Si el secreto no está, el despliegue **conserva** la que haya en la máquina y el gate avisa |

También existen las **variables** de repo (Settings → Secrets and variables →
Actions → pestaña *Variables* — no son secretos):

| Variable | Qué es |
|---|---|
| `SCHEDULER_RESEARCH_ENABLED` | el **apagado de emergencia** del researcher L3. Sin definirla queda encendido; fijarla en `false` lo apaga sin tocar código. Ojo: si existe con valor `false` de antes, anula el encendido por código — borrarla |
| `VECINO_URL` | dominio público del stack vecino, con esquema (`https://…`), para el gate de no-interferencia. Ausente o vacía ⇒ el gate omite el `curl` con aviso |
| `VECINO_FILTRO` | prefijo del nombre de sus contenedores, para `docker ps --filter name=…`. Ausente o vacía ⇒ se omite esa comprobación con aviso |

Las dos del vecino **no** siguen la regla de los tres sitios de abajo: no
entran a ningún contenedor — solo las lee `gates.sh`, exportadas en el bloque
stdin del paso Gates del workflow. Y no se guardan en el repo porque describen
un stack ajeno y el repositorio es público; por lo mismo, `gates.sh` no imprime
sus valores (las variables, a diferencia de los secretos, **no se enmascaran**
en los logs de Actions).

> **Ojo**: el compose no declara `env_file`, así que una variable sólo llega al
> contenedor si está en el mapa `environment:` del servicio. Añadir una nueva
> exige tocar tres sitios: el secreto/variable en GitHub, la plantilla de
> [`scripts/lib/entorno.sh`](../scripts/lib/entorno.sh) —**con su clase**— y el
> mapa del compose.

**5. Levantar el stack sin tocar Caddy**, y comprobar por túnel:

```bash
ssh -L 8011:127.0.0.1:8011 <usuario>@<vps>
```

Con el túnel abierto, `http://127.0.0.1:8011` desde el navegador local.

**6. El site block en el Caddyfile compartido** (vive en el repo del stack vecino):

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
docker exec <contenedor-caddy> caddy reload --config /etc/caddy/Caddyfile
```

Dejar una nota en ese Caddyfile apuntando aquí: el acoplamiento entre los dos repos tiene que estar escrito en los dos lados.

---

## Despliegue

Automático en push a `main`. Cada corrida queda registrada como *deployment* del entorno `produccion` — la sección «Deployments» de la portada del repo muestra el último, con fecha, estado y enlace al sitio. El [workflow](../.github/workflows/deploy.yml) no lleva la lógica dentro: manda [`scripts/desplegar.sh`](../scripts/desplegar.sh) y [`scripts/gates.sh`](../scripts/gates.sh) por stdin. Los secretos van por stdin y no como argumentos de `ssh` porque `ssh host VAR=x bash` los deja en la línea de comandos del proceso remoto, a la vista de cualquier `ps`.

Qué hace, en orden:

1. `git fetch` + `reset --hard` al commit. No `pull`: el estado del VPS lo fija el commit, no lo que alguien dejara ahí arreglando algo.
2. **Fusiona** `.env`: actualiza lo que gestiona y conserva el resto (ver «El `.env` de producción» más abajo). Ya no es la única copia — lo que sólo exista en la máquina sobrevive al despliegue.
3. `build` + `up -d` con el overlay de producción.
4. Espera a que la API esté `healthy` (o vuelca sus logs y aborta).
5. `alembic upgrade head`.
6. **Gates.** Cualquiera en rojo aborta:

| Gate | Qué comprueba |
|---|---|
| Deriva de esquema | `python -m core.schema_check`: la base está en el head **y** coincide con el ORM. Puede estar en el head y aun así diferir |
| Humo HTTP | `/healthz`, `/meta/frescura`, y la portada **con filas** — un 200 con la tabla vacía es justo lo que hay que ver — y que la canónica apunte al dominio y no al loopback |
| Llaves opcionales | Que `BANXICO_TOKEN` y `DEEPSEEK_API_KEY` **llegaron al contenedor** del scheduler. Avisa (⚠) sin bloquear: sin ellas el sitio sirve igual |
| No interferencia | Los contenedores del vecino (`VECINO_FILTRO`) siguen `healthy` y su dominio (`VECINO_URL`) responde 200. Sin las variables, se omite con aviso |
| TLS público | `https://centinelafinanciero.lat` responde 200 |

Los gates corren desde el repo que el despliegue acaba de actualizar, así que las comprobaciones son siempre las del commit que se está verificando.

---

## El `.env` de producción

El despliegue **fusiona** el `.env` del VPS: actualiza lo que gestiona, añade lo que falta y **conserva lo demás**. Antes lo reescribía entero desde un heredoc, y eso costó un incidente — una `DEEPSEEK_API_KEY` que sólo existía en la máquina se borró sola, el researcher falló días (quince instituciones por corrida) y se descubrió leyendo logs por casualidad.

La causa merece recordarse porque condiciona todo lo demás: `${{ secrets.X }}` de un secreto que no existe expande a **cadena vacía**, así que el workflow emite la línea igualmente y la variable llega al VPS *definida pero vacía*. Por eso lo que decide es que el valor **no esté vacío**, nunca que la variable esté definida.

Qué gana cada choque lo dice la clase de cada variable, declarada a su lado en [`scripts/lib/entorno.sh`](../scripts/lib/entorno.sh):

| Clase | Quién gana | Por qué |
|---|---|---|
| `fija` | El repo, siempre | `POSTGRES_HOST`, `LOG_LEVEL`… describen la topología de dentro del compose. Editarlas a mano es una avería, no una preferencia; si difieren se corrigen y el reporte lo dice con `(cambió)` |
| `github` | GitHub, siempre | Los secretos obligatorios nunca caen al valor del archivo: uno borrado debe **abortar** el despliegue, no resucitar callado. Una rotación que «no tuvo efecto» es el peor fallo posible |
| `opcional` | GitHub si trae valor; si no, se conserva | `BANXICO_TOKEN`, `DEEPSEEK_API_KEY`. Que el secreto no esté registrado significa «nunca lo puse», no «bórralo» |
| `interruptor` | GitHub si trae valor; si no, el default del código | `SCHEDULER_RESEARCH_ENABLED`. **No se conserva**, al revés que las llaves: borrar la variable de repo significa «vuelve al default», y conservarla dejaría un `false` viejo apagando el researcher para siempre. En una frase: se conservan credenciales, no interruptores |
| (lo demás) | El archivo, siempre | Claves ajenas, comentarios y líneas en blanco se copian **en su sitio**. Es lo que permite dejar una palanca puesta a mano sin que el despliegue se la lleve |

Cada despliegue imprime un bloque `── .env ──` con la procedencia de las dieciséis variables — de GitHub, conservada, por defecto o ausente — **sin imprimir un solo valor**: los logs de Actions de un repositorio público son públicos.

**Para retirar de verdad** una variable que ya no está en GitHub: Actions → deploy → *Run workflow* con `reescribir_env` marcado. Es un input del dispatch y no una variable de repo a propósito — una variable se quedaría encendida y arrasaría en el siguiente push. El archivo anterior queda en `.env.reemplazado`.

> **El `.env` del VPS no es un almacén de registro.** Conservar es una red contra la pérdida silenciosa, no el sitio donde deben vivir las llaves: ese archivo no se respalda (`respaldar.sh` sólo vuelca Postgres), no existe en una máquina nueva, y nadie puede auditar qué llave usa producción mirando GitHub. Por eso el gate insiste con su ⚠ en cada despliegue.

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

Retención de 14 días, nombre con prefijo propio para no pisarse con los de NA. El volcado se escribe a un temporal y sólo se renombra al terminar: un corte a la mitad no deja un `.sql.gz` truncado con pinta de respaldo bueno. La rotación va **después** del volcado, nunca antes.

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

## Navegador en el VPS — decisión aplicada

**Estado: aplicada.** Aplazada el 2026-07-29 por la RAM; revertida la
prórroga el 2026-08-01 por decisión del propietario, **condicionada a la
medición de abajo antes del merge**.

Once de las dieciocho fuentes de tasas se pintan con JavaScript. Chromium
viaja ahora en la imagen (capa propia del [Dockerfile](../docker/app/Dockerfile),
`playwright install --with-deps chromium` con `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright`
para que el uid sin privilegios pueda leerlo), el job de tasas arma su cadena
httpx + navegador y cubre las dieciocho fuentes, y el límite del scheduler
subió a **768M** en el overlay. La pasada semanal desde la laptop deja de ser
un modo de operación; los flags `--solo-navegador` / `--sin-navegador` de la
CLI quedan como filtros de depuración.

> **Cadencia (2026-08-01):** el job corre **cada 4 horas** (rejilla ..:45)
> para probar el ciclo completo; el destino es bajarlo a diario editando el
> trigger en `src/scheduler/registry.py`. Chromium levanta ahora seis veces
> al día en vez de una a la semana — vigilar `free -h` (`available`) los
> primeros días. El costo LLM no se multiplica: el cortocircuito por hash
> hace gratis las corridas sin cambios, y el techo `llm_cost_daily_limit_usd`
> es diario y compartido (agotado ⇒ OMITIDO, no FALLIDO).

**El repliegue, en dos niveles:**

1. **Sin deploy** — si la RAM protesta (OOM del scheduler, `available` en
   caída): `docker compose exec -T api python -m cli config set
   tasas_fetch_solo_sin_js true --motivo "RAM medida AAAA-MM-DD"`. El job
   vuelve a solo-httpx y Chromium ni se construye.
2. **Revert** — los tres últimos commits de la cola (docs, flip del default,
   imagen): Actions → deploy → Run workflow con el SHA anterior a la cola en
   `ref`, o `git revert` del rango. No hay migraciones en la cola.

**Medición** (la condición del 768M; este registro es el rastro que el
aplazamiento nunca tuvo):

```
Medición: pendiente de registrar.
  - free -h (columna available, 3-4 muestras en un día normal): ____
  - pico de MEM del scheduler durante una corrida con navegador
    (docker stats centinela-scheduler-1 en paralelo a
     docker compose ... exec -T scheduler python -m cli tasas fetch): ____
  - duración de la corrida completa: ____
```

Criterio: **≥ 1 GiB en `available` de forma sostenida** (`available`, no
`free`: `free` no cuenta el page cache recuperable y subestima lo que de
verdad hay), y el pico del scheduler lejos del límite. Si no se cumple,
aplicar el repliegue de arriba.

---

## Cosas que muerden en este VPS

- **`ufw` dropea `docker0 → host`.** Un contenedor de Centinela en bridge no alcanza nada publicado en el loopback del host. Si algún día Centinela necesita consumir un servicio del vecino, la vía es adjuntar el contenedor a la red bridge del vecino como red externa, nunca pasar por la gateway de Docker.
- **Centinela no levanta Caddy.** 80/443 los tiene el de NA en `network_mode: host`.
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
