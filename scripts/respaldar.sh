#!/usr/bin/env bash
# Respaldo de la base. Pensado para cron diario en el VPS:
#
#   15 3 * * *  /home/<usuario>/centinela-financiero/scripts/respaldar.sh >> \
#               /home/<usuario>/centinela-financiero/backups/respaldo.log 2>&1
#
# Nombre con prefijo propio y directorio propio: en este VPS también respalda
# NarrativeAlpha, y dos rotaciones que se pisen borran lo que no es suyo.
set -euo pipefail

DIRECTORIO=${DIRECTORIO:-$HOME/centinela-financiero}
DESTINO=${DESTINO:-$DIRECTORIO/backups}
CONTENEDOR=${CONTENEDOR:-centinela-db}
USUARIO=${POSTGRES_USER:-centinela}
BASE=${POSTGRES_DB:-centinela}
RETENCION_DIAS=${RETENCION_DIAS:-14}

mkdir -p "$DESTINO"
marca=$(date +%Y%m%d-%H%M%S)
archivo="$DESTINO/centinela-$marca.sql.gz"

echo "[$(date -Is)] respaldando $BASE desde $CONTENEDOR"

# A un temporal y sólo al final al nombre definitivo: si el volcado se corta a
# la mitad, no queda un .sql.gz truncado con pinta de respaldo bueno.
temporal="$archivo.parcial"
if ! docker exec "$CONTENEDOR" pg_dump -U "$USUARIO" -d "$BASE" --no-owner --clean --if-exists \
  | gzip -9 > "$temporal"; then
  echo "[$(date -Is)] ERROR: el volcado falló" >&2
  rm -f "$temporal"
  exit 1
fi
mv "$temporal" "$archivo"

# Un gzip vacío pesa 20 bytes. Cualquier cosa por debajo de 1 KB no es una base.
tamano=$(stat -c%s "$archivo")
if [ "$tamano" -lt 1024 ]; then
  echo "[$(date -Is)] ERROR: el respaldo pesa $tamano bytes, no puede estar bien" >&2
  rm -f "$archivo"
  exit 1
fi

echo "[$(date -Is)] listo: $archivo ($((tamano / 1024)) KB)"

# La rotación va después de un respaldo bueno, nunca antes: si el volcado falla,
# lo que hay viejo es lo único que queda y no se toca.
borrados=$(find "$DESTINO" -maxdepth 1 -name 'centinela-*.sql.gz' -mtime "+$RETENCION_DIAS" -print -delete | wc -l)
echo "[$(date -Is)] rotación: $borrados archivos de más de $RETENCION_DIAS días"
echo "[$(date -Is)] en disco: $(find "$DESTINO" -maxdepth 1 -name 'centinela-*.sql.gz' | wc -l) respaldos"
