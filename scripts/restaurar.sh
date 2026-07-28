#!/usr/bin/env bash
# Restauración de un respaldo.
#
#   scripts/restaurar.sh backups/centinela-20260728-031500.sql.gz
#
# Por defecto restaura en un Postgres **desechable** levantado para la ocasión,
# que es como se prueba un respaldo sin arriesgar nada. Para escribir sobre una
# base real hay que pedirlo explícitamente:
#
#   CONTENEDOR=centinela-db CONFIRMO_SOBRESCRIBIR=si scripts/restaurar.sh <archivo>
#
# Un respaldo que nunca se restauró no es un respaldo: es un archivo.
set -euo pipefail

ARCHIVO=${1:?uso: restaurar.sh <archivo.sql.gz>}
USUARIO=${POSTGRES_USER:-centinela}
BASE=${POSTGRES_DB:-centinela}
CONTENEDOR=${CONTENEDOR:-}

[ -f "$ARCHIVO" ] || { echo "no existe $ARCHIVO" >&2; exit 1; }
gzip -t "$ARCHIVO" || { echo "$ARCHIVO no es un gzip íntegro" >&2; exit 1; }
echo "archivo íntegro: $ARCHIVO ($(($(stat -c%s "$ARCHIVO") / 1024)) KB)"

if [ -n "$CONTENEDOR" ]; then
  if [ "${CONFIRMO_SOBRESCRIBIR:-}" != "si" ]; then
    cat >&2 <<AVISO
Vas a restaurar sobre '$CONTENEDOR', y el volcado trae DROP de cada tabla.
Si es lo que quieres, repite con CONFIRMO_SOBRESCRIBIR=si.
AVISO
    exit 1
  fi
  echo "restaurando sobre $CONTENEDOR"
  gunzip -c "$ARCHIVO" | docker exec -i "$CONTENEDOR" psql -q -U "$USUARIO" -d "$BASE"
  echo "restaurado"
  exit 0
fi

# ── Prueba en un contenedor desechable ──
efimero="centinela-restore-prueba-$$"
echo "levantando $efimero para probar la restauración"
docker run -d --rm --name "$efimero" \
  -e POSTGRES_USER="$USUARIO" -e POSTGRES_DB="$BASE" -e POSTGRES_PASSWORD=prueba \
  postgres:16 > /dev/null
limpiar() { docker rm -f "$efimero" > /dev/null 2>&1 || true; }
trap limpiar EXIT

for _ in $(seq 1 30); do
  docker exec "$efimero" pg_isready -U "$USUARIO" -d "$BASE" > /dev/null 2>&1 && break
  sleep 1
done

gunzip -c "$ARCHIVO" | docker exec -i "$efimero" psql -q -U "$USUARIO" -d "$BASE"

echo
echo "filas por tabla en la base restaurada:"
docker exec "$efimero" psql -qtAX -U "$USUARIO" -d "$BASE" -c "
  SELECT '  ' || relname || ': ' || n_live_tup
  FROM pg_stat_user_tables
  WHERE n_live_tup > 0
  ORDER BY relname;"

# La prueba no es que el psql no diera error: es que los datos estén ahí.
instituciones=$(docker exec "$efimero" psql -qtAX -U "$USUARIO" -d "$BASE" \
  -c "SELECT count(*) FROM instituciones;")
echo
if [ "${instituciones:-0}" -gt 0 ]; then
  echo "✓ restauración correcta: $instituciones instituciones"
else
  echo "✗ la base restaurada no tiene instituciones" >&2
  exit 1
fi
