#!/usr/bin/env bash
# Gates del despliegue. Cualquiera en rojo aborta.
#
# Corre en el VPS, desde el repo ya actualizado por `desplegar.sh`, así que la
# versión de estas comprobaciones es siempre la del commit que se acaba de
# desplegar — no la de cuando se escribió el workflow.
#
#   { echo "API_READ_KEY=..."; cat scripts/gates.sh; } | ssh vps 'bash -s'
#
# Cuidado con `< /dev/null` más abajo: este script llega **por stdin**, y bash
# lo lee a medida que lo ejecuta. Un `docker compose exec -T` sin redirigir se
# traga lo que queda del archivo, el script termina ahí y sale 0 — un gate que
# aprueba porque se quedó sin texto que leer.
set -euo pipefail

: "${API_READ_KEY:?falta API_READ_KEY}"

DIRECTORIO=${DIRECTORIO:-$HOME/centinela-financiero}
DOMINIO=${DOMINIO:-https://centinelafinanciero.lat}
VECINO=${VECINO:-https://narrative-alpha.cloud}

cd "$DIRECTORIO"

echo "════════ deriva de esquema ════════"
# `docker compose exec` a secas, sin el overlay de producción: el contenedor ya
# está corriendo y sólo hace falta entrar en él. Arrastrar el overlay ataba
# este gate a que `SITE_URL` estuviera en el entorno de quien lo lanza, y
# entonces fallaba por una razón que no tiene nada que ver con lo que mide.
# Puede estar en el head de las migraciones y aun así diferir del ORM.
docker compose exec -T api python -m core.schema_check < /dev/null

echo
echo "════════ humo HTTP ════════"
curl -fsS http://127.0.0.1:8010/healthz > /dev/null
echo "  /healthz            ok"

curl -fsS -H "X-API-Key: ${API_READ_KEY}" \
  http://127.0.0.1:8010/api/v1/meta/frescura > /tmp/frescura.json
echo "  /meta/frescura      ok"

curl -fsS http://127.0.0.1:8011/ > /tmp/portada.html
# Un 200 con la tabla vacía es justo el fallo que este gate existe para ver:
# la API responde, el sitio compila, y no hay ni un dato que mostrar.
# `grep -o | wc -l` y no `grep -c`: el segundo cuenta líneas y el HTML trae
# varias filas por línea, así que informaría de menos.
filas=$(grep -o 'class="fila"' /tmp/portada.html | wc -l)
echo "  portada             ok, $filas filas"
if [ "${filas:-0}" -eq 0 ]; then
  echo "  ✗ la portada no trae ni una fila"
  exit 1
fi

# Si SITE_URL no llegó al contenedor, el sitio se anuncia a los buscadores con
# la URL del loopback y nadie se entera hasta que el índice está hecho.
if ! grep -q "rel=\"canonical\" href=\"${DOMINIO}" /tmp/portada.html; then
  echo "  ✗ la canónica no apunta a ${DOMINIO}: revisa SITE_URL"
  grep -o 'rel="canonical" href="[^"]*"' /tmp/portada.html || true
  exit 1
fi
echo "  canónica            ok, ${DOMINIO}"

echo
echo "════════ no interferir con NarrativeAlpha ════════"
# Compartimos máquina y Caddy. Un despliegue de Centinela que tumbe al vecino
# es un fallo de Centinela, aunque Centinela funcione.
docker ps --filter 'name=narrativealpha' --format '  {{.Names}}  {{.Status}}' < /dev/null
enfermos=$(
  docker ps --filter 'name=narrativealpha' --format '{{.Names}} {{.Status}}' < /dev/null |
    grep -v '(healthy)' | grep -v 'caddy' || true
)
if [ -n "$enfermos" ]; then
  echo "  ✗ contenedores del vecino fuera de healthy:"
  echo "$enfermos"
  exit 1
fi

codigo=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$VECINO")
echo "  $VECINO → $codigo"
[ "$codigo" = 200 ] || { echo "  ✗ el dominio del vecino no responde 200"; exit 1; }

echo
echo "════════ TLS público ════════"
codigo=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$DOMINIO")
echo "  $DOMINIO → $codigo"
[ "$codigo" = 200 ] || { echo "  ✗ el sitio no responde 200 por TLS"; exit 1; }

echo
echo "✓ todos los gates en verde"
