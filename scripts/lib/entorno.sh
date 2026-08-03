#!/usr/bin/env bash
# Fusión del `.env` de producción. **No ejecuta nada al cargarse**: sólo define
# funciones, y ninguna toca git, docker ni la red. Eso es lo que la hace
# probable desde pytest — hasta ahora, la primera vez que alguien ejecutaba la
# lógica del despliegue era en producción.
#
# Viaja concatenada DELANTE de `desplegar.sh` por la misma tubería `bash -s`
# (paso «Desplegar» de .github/workflows/deploy.yml). Y por ahí, no con un
# `source` del árbol del VPS: en un rollback el workflow ejecuta el script de
# `main` contra un árbol reseteado a otro commit, así que un `source` habría
# abortado todo rollback a un commit anterior a este cambio. Viajando por la
# tubería, script y librería son siempre del mismo commit.
#
# De venir concatenada salen tres prohibiciones que no son estilo: aquí no
# puede haber un `exit` —se llevaría el resto del despliegue—, ni nada que lea
# stdin —se tragaría el script que viene detrás, y la corrida terminaría en
# silencio con salida 0—, ni efectos al cargarse.
#
# Por qué existe: el heredoc que había en `desplegar.sh` reescribía el archivo
# entero en cada corrida, y `deploy.yml` emite las opcionales SIEMPRE, porque
# un `${{ secrets.X }}` que no existe expande a cadena vacía. La llave de
# DeepSeek que el dueño tenía puesta a mano en la máquina se borró sola; el
# researcher falló días, quince instituciones por corrida, y se descubrió
# leyendo logs por casualidad. De ahí la regla que gobierna todo este archivo:
# lo que decide es que el valor NO esté vacío, jamás que la variable esté
# definida. `[ -n "${!clave:-}" ]`, nunca `[ -v clave ]`.

# Cada línea: CLAVE, clase, y valor por defecto si la clase lo usa. El orden es
# el del archivo cuando hay que crearlo de cero.
#
# Las clases, y quién gana cuando el valor de GitHub y el del archivo chocan:
#
#   fija         Topología de dentro del compose. Manda el despliegue, siempre:
#                un POSTGRES_HOST editado a mano es una avería, no una
#                preferencia — el contenedor no alcanza el loopback del host.
#   github       Secreto obligatorio. Manda GitHub, siempre, y nunca cae al
#                valor del archivo: un secreto borrado tiene que abortar el
#                despliegue (guardas `:?` de desplegar.sh) y no resucitar
#                callado con lo que hubiera en la máquina. Una rotación que
#                «no tuvo efecto» es peor que un despliegue que falla.
#   opcional     Secreto que puede no estar registrado en GitHub. Gana GitHub
#                **si trae valor**; vacío conserva lo que ya hubiera. Ésta es
#                la clase del incidente.
#   interruptor  Bandera de operación con default en el código. Gana GitHub si
#                trae valor; vacío vuelve al default y NO conserva. Al revés
#                que `opcional`, y a propósito: borrar la variable de repo
#                significa «vuelve al default», así que conservar dejaría un
#                `false` viejo apagando el researcher para siempre — la trampa
#                que docs/despliegue.md ya documenta. En una frase: se
#                conservan credenciales, no interruptores.
ENTORNO_PLANTILLA=(
  "ENVIRONMENT                fija        prod"
  "POSTGRES_HOST              fija        db"
  "POSTGRES_PORT              fija        5432"
  "POSTGRES_DB                fija        centinela"
  "POSTGRES_USER              fija        centinela"
  "POSTGRES_PASSWORD          github"
  "REDIS_HOST                 fija        redis"
  "REDIS_PORT                 fija        6379"
  "API_READ_KEY               github"
  "API_ADMIN_KEY              github"
  "SITE_URL                   github"
  "LOG_LEVEL                  fija        INFO"
  "LOG_FORMAT                 fija        json"
  "BANXICO_TOKEN              opcional"
  "DEEPSEEK_API_KEY           opcional"
  "SCHEDULER_RESEARCH_ENABLED interruptor true"
)

# Resultados de `entorno_fusionar`. Declarados aquí y no dentro de la función
# para que `entorno_reportar` no explote bajo `set -u` si alguien la llama sola.
ENTORNO_SALIDA=()
ENTORNO_DESCONOCIDAS=()
ENTORNO_AVISOS=()
ENTORNO_VALOR=""
ENTORNO_PROCEDENCIA=""
declare -A ENTORNO_PROCEDENCIAS=()

# Decide el valor de UNA variable. Deja el resultado en ENTORNO_VALOR y de
# dónde salió en ENTORNO_PROCEDENCIA.
#
# La expansión indirecta `${!clave:-}` resuelve el valor **en este mismo
# shell**, y eso no es una floritura: el workflow manda las variables como
# asignaciones a secas (`DEEPSEEK_API_KEY='...'`), no exportadas, y este script
# se concatena a ese mismo `bash -s`. Un `awk`, un `python` o un `env` hijo no
# las vería y escribiría el .env con todo en blanco — que es exactamente el
# fallo que este archivo existe para no repetir. El `:-` tampoco sobra: sin él
# la expansión indirecta de una variable no definida aborta bajo `set -u`.
entorno_resolver() {  # <clase> <clave> <defecto>  [previo]
  local clase=$1 clave=$2 defecto=${3-} previo=${4-}
  local de_github=${!clave:-}

  case "$clase" in
    fija)
      ENTORNO_VALOR=$defecto
      ENTORNO_PROCEDENCIA=fija
      ;;
    github)
      ENTORNO_VALOR=$de_github
      ENTORNO_PROCEDENCIA="de github"
      ;;
    opcional)
      if [ -n "$de_github" ]; then
        ENTORNO_VALOR=$de_github
        ENTORNO_PROCEDENCIA="de github"
      elif [ -n "$previo" ]; then
        ENTORNO_VALOR=$previo
        ENTORNO_PROCEDENCIA="conservada del .env"
      else
        ENTORNO_VALOR=""
        ENTORNO_PROCEDENCIA="ausente"
      fi
      ;;
    interruptor)
      if [ -n "$de_github" ]; then
        ENTORNO_VALOR=$de_github
        ENTORNO_PROCEDENCIA="de github"
      else
        ENTORNO_VALOR=$defecto
        ENTORNO_PROCEDENCIA="por defecto"
      fi
      ;;
    *)
      echo "clase desconocida '$clase' para $clave" >&2
      return 1
      ;;
  esac
}

# Lo que el valor no puede ser, y lo que el valor no debería ser.
#
# El salto de línea aborta: un secreto de GitHub con un salto (un pegado con
# una línea de más) llega aquí como variable multilínea y partiría el .env en
# dos, dejando media línea suelta que la próxima fusión conservaría como
# «desconocida» para siempre.
#
# El « #» sólo avisa: el parser dotenv de docker compose corta ahí un valor sin
# comillas. Es una limitación que ya existía con el heredoc, no una regresión,
# y bloquear el despliegue por ella castigaría a quien no la causó. Se escribe
# en crudo, sin comillas, exactamente como el heredoc: entrecomillar cambiaría
# lo que el compose interpreta para valores que hoy ya funcionan —empezando por
# POSTGRES_PASSWORD, que tiene que seguir coincidiendo con la del volumen.
entorno_validar() {  # <clave> <valor> [valor_en_el_archivo]
  local clave=$1 valor=$2 previo=${3-} salto=$'\n'

  case "$valor" in
    *"$salto"*)
      echo "  ✗ $clave trae un salto de línea; revisa el secreto en GitHub" >&2
      return 1
      ;;
  esac
  case "$valor" in
    *" #"*) ENTORNO_AVISOS+=("$clave lleva « #»: docker compose corta el valor ahí") ;;
  esac
  case "$valor" in
    '"'* | "'"*) ENTORNO_AVISOS+=("$clave empieza por comilla: docker compose se la quita") ;;
  esac

  # Que el valor cambió es información sobre el hecho, no sobre el contenido:
  # es la única pista que existirá el día que alguien rote la contraseña y el
  # volumen de Postgres siga con la vieja. Y en una `fija` delata una edición a
  # mano que el despliegue acaba de pisar.
  if [ -n "$previo" ] && [ "$valor" != "$previo" ]; then
    ENTORNO_PROCEDENCIA="$ENTORNO_PROCEDENCIA (cambió)"
  fi
}

# Construye el .env nuevo en ENTORNO_SALIDA. **No escribe nada**: separar
# construir de escribir es lo que permite ensayar la fusión contra una copia
# del .env real sin tocarlo.
entorno_fusionar() {  # <archivo>
  local archivo=$1
  local -a lineas=()
  local -A clase_de=() defecto_de=() previo_de=() visto=()
  local entrada clase clave defecto linea

  for entrada in "${ENTORNO_PLANTILLA[@]}"; do
    read -r clave clase defecto <<< "$entrada"
    clase_de[$clave]=$clase
    defecto_de[$clave]=$defecto
  done

  # `< "$archivo"` explícito, porque este script llega por stdin y un `read`
  # sin redirigir se comería el resto del despliegue. Y `|| [ -n "$linea" ]`
  # para no perder la última línea si el archivo no acaba en salto: un .env
  # editado a mano puede no acabar en salto, y esa última línea suele ser justo
  # la que alguien añadió.
  if [ -f "$archivo" ]; then
    while IFS= read -r linea || [ -n "$linea" ]; do
      lineas+=("$linea")
    done < "$archivo"
  fi

  # Primera pasada: qué había. Se queda la PRIMERA aparición de cada clave, que
  # es también la que sobrevive en la segunda pasada.
  for linea in "${lineas[@]}"; do
    clave=${linea%%=*}
    [ "$clave" = "$linea" ] && continue          # línea sin `=`
    case "$linea" in "#"*) continue ;; esac      # comentario
    [[ $clave =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    [ -n "${clase_de[$clave]:-}" ] || continue
    [ -n "${previo_de[$clave]+x}" ] || previo_de[$clave]=${linea#*=}
  done

  ENTORNO_SALIDA=()
  ENTORNO_DESCONOCIDAS=()
  ENTORNO_AVISOS=()
  ENTORNO_PROCEDENCIAS=()

  # Segunda pasada: el archivo se reescribe línea a línea EN SU ORDEN. Las que
  # el despliegue gestiona se sustituyen en el sitio; las demás pasan tal cual.
  # Reagrupar por clases daría un archivo más bonito y dejaría cada comentario
  # del dueño lejos de lo que comentaba.
  for linea in "${lineas[@]}"; do
    clave=${linea%%=*}
    if [ "$clave" = "$linea" ] || [ -z "$clave" ] ||
      ! [[ $clave =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
      ENTORNO_SALIDA+=("$linea")
      continue
    fi
    if [ -z "${clase_de[$clave]:-}" ]; then
      ENTORNO_SALIDA+=("$linea")
      ENTORNO_DESCONOCIDAS+=("$clave")
      continue
    fi
    # Duplicada: el compose se queda con la última, así que dejar dos es dejar
    # que el archivo diga una cosa y el contenedor lea otra. Se colapsa en la
    # primera, con el valor resuelto, y se avisa.
    if [ -n "${visto[$clave]:-}" ]; then
      ENTORNO_AVISOS+=("$clave estaba repetida en el .env: se dejó una sola")
      continue
    fi
    visto[$clave]=1
    entorno_resolver "${clase_de[$clave]}" "$clave" "${defecto_de[$clave]}" \
      "${previo_de[$clave]:-}" || return 1
    entorno_validar "$clave" "$ENTORNO_VALOR" "${linea#*=}" || return 1
    ENTORNO_SALIDA+=("$clave=$ENTORNO_VALOR")
    ENTORNO_PROCEDENCIAS[$clave]=$ENTORNO_PROCEDENCIA
  done

  # Lo que la plantilla tiene y el archivo no: al final, en el orden de la
  # plantilla. En un .env recién creado esto es el archivo entero.
  for entrada in "${ENTORNO_PLANTILLA[@]}"; do
    read -r clave clase defecto <<< "$entrada"
    [ -n "${visto[$clave]:-}" ] && continue
    entorno_resolver "$clase" "$clave" "$defecto" "" || return 1
    entorno_validar "$clave" "$ENTORNO_VALOR" "" || return 1
    ENTORNO_SALIDA+=("$clave=$ENTORNO_VALOR")
    ENTORNO_PROCEDENCIAS[$clave]=$ENTORNO_PROCEDENCIA
  done
}

# Escritura atómica de ENTORNO_SALIDA.
entorno_escribir() {  # <archivo>
  # Dos `local` y no uno con las dos asignaciones: bash expande TODOS los
  # argumentos de `local` antes de aplicar el primero, así que un
  # `local archivo=$1 temporal="$archivo..."` busca `archivo` en el ámbito de
  # fuera y aborta con «unbound variable» bajo `set -u`.
  local archivo=$1
  local temporal="$archivo.nuevo.$$"

  # A un temporal y sólo al final al nombre definitivo, como respaldar.sh con
  # el volcado: un `cat >` que se corte a la mitad deja un .env truncado que el
  # compose lee igual de contento, y lo que falta son variables sin las que los
  # contenedores arrancan «bien» y funcionan mal.
  #
  # El `umask 077` va en un subshell y ANTES de crear el temporal: un umask
  # sólo afecta a archivos nuevos, y el `mv` traslada al destino el modo del
  # temporal. Puesto después no serviría de nada y el .env acabaría legible por
  # cualquier cuenta de una máquina que compartimos con otro stack.
  if ! (
    umask 077
    printf '%s\n' "${ENTORNO_SALIDA[@]}" > "$temporal"
  ); then
    rm -f "$temporal"
    echo "  ✗ no se pudo escribir $temporal" >&2
    return 1
  fi
  mv "$temporal" "$archivo"
}

# Qué se escribió y de dónde salió cada cosa. Nombres y procedencias, **nunca
# valores**: ni completos, ni truncados, ni «los primeros cuatro caracteres» —
# un prefijo de una API key sigue siendo material de una API key, y los logs de
# Actions de un repositorio público son públicos.
#
# Se imprimen las dieciséis siempre, no sólo las raras: un reporte que sólo
# habla cuando hay problema entrena a no leerlo, y el valor de este bloque es
# poder comparar dos despliegues.
entorno_reportar() {
  local entrada clave clase defecto aviso

  for entrada in "${ENTORNO_PLANTILLA[@]}"; do
    read -r clave clase defecto <<< "$entrada"
    printf '  %-26s %s\n' "$clave" "${ENTORNO_PROCEDENCIAS[$clave]:-sin resolver}"
  done
  if [ "${#ENTORNO_DESCONOCIDAS[@]}" -gt 0 ]; then
    printf '  %-26s %s\n' "(ajenas)" \
      "${#ENTORNO_DESCONOCIDAS[@]} conservadas: ${ENTORNO_DESCONOCIDAS[*]}"
  fi
  for aviso in "${ENTORNO_AVISOS[@]}"; do
    echo "  ⚠ $aviso"
  done
}

# La escotilla: aparta el .env en vez de borrarlo. `.env.reemplazado` ya está
# cubierto por el `.env.*` de .gitignore y el de .dockerignore, así que ni se
# versiona ni entra al contexto de build.
entorno_apartar() {  # <archivo>
  local archivo=$1
  [ -f "$archivo" ] || return 0
  mv "$archivo" "$archivo.reemplazado"
}
