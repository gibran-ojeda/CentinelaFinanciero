#!/usr/bin/env bash
# Despliegue en el VPS. Se ejecuta **por stdin**:
#
#   { echo "REF=..."; echo "POSTGRES_PASSWORD=..."; cat scripts/desplegar.sh; } \
#     | ssh vps 'bash -s'
#
# Por stdin y no como argumentos de `ssh` a propósito: `ssh host VAR=x bash`
# deja los secretos en la línea de comandos del proceso remoto, visibles en un
# `ps` para cualquiera con cuenta en la máquina.
#
# Variables que espera: REF, POSTGRES_PASSWORD, API_READ_KEY, API_ADMIN_KEY,
# SITE_URL. Opcionales: BANXICO_TOKEN y DEEPSEEK_API_KEY — sin ellas el stack
# sirve igual, sólo quedan sin llave los jobs de las fases 7 y 9 — y
# SCHEDULER_RESEARCH_ENABLED, el gate frío del researcher L3 (vacía = true;
# ponerla en false como variable de repo es el apagado de emergencia).
#
# Llega también `scripts/lib/entorno.sh`, concatenada por delante: es la que
# decide cómo se fusiona el .env de la máquina con lo que llega de GitHub.
#
# Ojo con los `< /dev/null`: bash lee este archivo de stdin a medida que lo
# ejecuta. Un comando que consuma stdin se traga lo que queda del script y la
# ejecución termina ahí, en silencio y con salida 0.
set -euo pipefail

: "${REF:?falta REF}"
: "${POSTGRES_PASSWORD:?falta POSTGRES_PASSWORD}"
: "${API_READ_KEY:?falta API_READ_KEY}"
: "${API_ADMIN_KEY:?falta API_ADMIN_KEY}"
: "${SITE_URL:?falta SITE_URL}"

DIRECTORIO=${DIRECTORIO:-$HOME/centinela-financiero}
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"

cd "$DIRECTORIO"

# `reset --hard` y no `pull`: el estado del VPS lo fija el commit, no lo que
# alguien haya dejado ahí arreglando algo a las tres de la mañana.
git fetch origin --prune --tags < /dev/null
git reset --hard "$REF" < /dev/null
echo "desplegando $(git rev-parse --short HEAD) — $(git log -1 --pretty=%s)"

# La librería del .env llega ya definida: el workflow la concatena delante de
# este script, en la misma tubería. Este `source` es la red para cuando alguien
# corre el despliegue a mano dentro de la máquina, y va después del
# `reset --hard` a propósito — la librería que se use es la del commit que se
# está desplegando, igual que pasa con los gates.
if ! declare -F entorno_fusionar > /dev/null; then
  . scripts/lib/entorno.sh 2> /dev/null || {
    echo "falta scripts/lib/entorno.sh; ¿el árbol es de un commit anterior al cambio?" >&2
    exit 1
  }
fi

# El .env se **fusiona**, no se regenera. Aquí había un heredoc que reescribía
# el archivo entero, y `deploy.yml` emite las opcionales aunque el secreto no
# exista: la llave de DeepSeek puesta a mano en la máquina se borró sola y el
# researcher falló días sin que el despliegue dijera una palabra. Quién gana
# cada choque está en scripts/lib/entorno.sh, junto a cada variable.
echo "── .env ──"
if [ "${CONFIRMO_REESCRIBIR_ENV:-}" = si ]; then
  # Para retirar de verdad una variable que ya no está en GitHub. Deliberado y
  # de una sola corrida: llega por el input del dispatch, no por una variable
  # de repo que se quedaría encendida y arrasaría en el siguiente push. La
  # forma es la de restaurar.sh, por la misma razón que allí.
  echo "  reescritura completa pedida; el .env anterior queda en .env.reemplazado"
  entorno_apartar .env
fi
entorno_fusionar .env
entorno_escribir .env
entorno_reportar

$COMPOSE build < /dev/null
$COMPOSE up -d --remove-orphans < /dev/null

echo "── esperando a que la API esté sana ──"
estado=starting
for _ in $(seq 1 40); do
  estado=$(docker inspect -f '{{.State.Health.Status}}' centinela-api 2>/dev/null </dev/null || echo starting)
  [ "$estado" = healthy ] && break
  sleep 3
done
if [ "$estado" != healthy ]; then
  echo "la API no llegó a healthy (quedó en '$estado')"
  $COMPOSE logs --tail=60 api < /dev/null
  exit 1
fi

echo "── migraciones ──"
$COMPOSE exec -T api alembic upgrade head < /dev/null

# Migrar crea las tablas; no las llena. Y una base vacía no es un despliegue a
# medias silencioso: sin parámetros fiscales, `_params_fiscales` devuelve 503 en
# todo lo que calcula —comparador, calculadora, instituciones—, la portada no
# renderiza, el healthcheck de `web` nunca pasa a healthy y el gate de humo
# muere. El primer despliegue en un VPS limpio es exactamente ese caso.
#
# Corre en cada despliegue y no sólo en el primero porque las dos son
# idempotentes: `seed` hace upsert por clave natural y `tasas import` salta las
# observaciones que ya existen (las cuenta como «ya existentes»). Así el
# catálogo del VPS es siempre el del commit desplegado, sin un paso manual que
# alguien tenga que recordar.
echo "── catálogo semilla ──"
$COMPOSE exec -T api python -m cli seed < /dev/null
$COMPOSE exec -T api python -m cli tasas import seeds/tasas.csv < /dev/null

echo "── despliegue aplicado; faltan los gates ──"
