# Runbook: actualización manual de tasas

> El ciclo semanal que mantiene vivo el catálogo hasta que las fases 7–9 automaticen la ingesta. Toma entre 20 y 45 minutos.

## Por qué a mano, y hasta cuándo

Las tasas de SOFIPOs y bancos digitales no tienen API: se publican en la página de cada institución, en tablas que cambian de sitio con cada rediseño. La [§15 del foundation](../foundation-comparador-financiero-mx.md) descarta el scraping por selectores CSS y lo resuelve en la **fase 9** con fetch dirigido y extracción por LLM. Mientras tanto, lo hace una persona.

Ocho de las páginas del catálogo no rinden contenido sin ejecutar JavaScript. Hay que abrirlas en un navegador de verdad; un cliente HTTP plano devuelve 403 o una página vacía.

---

## El ciclo

### 1. Qué falta

```bash
python -m cli tasas pendientes
```

Imprime, agrupado por institución, lo que **no puede salir al sitio público**: productos con tasa sin verificar y productos sin ninguna tasa. Cada bloque trae la URL oficial que hay que abrir, y marca las que necesitan navegador.

La URL sale de `seeds/fuentes_tasas.yaml`, no de la tasa guardada: esa última dice de dónde salió el dato la vez anterior, que es justo lo que se está corrigiendo.

### 2. Abrir cada página y anotar

De cada producto hace falta: **tasa nominal anual**, **plazo en días**, **GAT nominal y real si la institución las publica**, y la **fecha** que aparezca en la página (fecha de cálculo de la GAT, o la del día si no hay otra).

Tres cosas que se ven seguido y hay que resistir:

- **«Hasta 15 %»** no es una tasa: es el techo de un tramo o de un segmento de cliente. Si la página no dice a qué plazo y a qué monto corresponde, **no se captura**.
- **Los plazos son los de la institución**, no los de CETES. Si Finsus publica 30/90/180/360, el producto es de 30 días — no de 28 porque CETES lo sea.
- **Una GAT que no cuadra con la nominal** no se corrige a ojo: se captura tal como la publica la institución. Si es inconsistente, la bandera 🟡 de §5.2 está para decirlo.

### 3. Actualizar el CSV

Cada fila de `seeds/tasas.csv` es una **observación nueva**, no una edición. La tabla es append-only: la vigente de un producto es la más reciente en estado `VIGENTE`.

```csv
producto_slug,tasa_nominal,gat_nominal,gat_real,fecha_dato,fuente,fuente_url,estado,notas
finsus-plazo-360,8.69,8.69,4.56,2026-07-28,MANUAL,https://www.finsus.mx/inversion,VIGENTE,Fecha de calculo de la GAT: 02 de julio de 2026.
```

- `fuente_url` es **la página de la institución**. Se pinta como enlace en el sitio, así que tiene que ser legible por una persona: nunca un endpoint de API que devuelva JSON.
- `estado=VIGENTE` sólo si se leyó de la propia institución. Si el dato viene de un agregador o hay dudas, `PENDIENTE_REVISION` — y entonces no sale al sitio público, que es lo correcto.
- Lo que no se pudo verificar **se deja como está**. Una tasa vieja marcada es mejor que una inventada.

### 4. Importar

```bash
python -m cli tasas import seeds/tasas.csv --dry-run   # primero en seco
python -m cli tasas import seeds/tasas.csv
```

En el VPS, dentro del contenedor:

```bash
cd ~/centinela-financiero
docker compose exec -T api python -m cli tasas import seeds/tasas.csv
```

La clave natural es `(producto, fecha_dato, fuente)`: reimportar el mismo CSV no duplica nada.

### 5. Comprobar en el sitio

```bash
curl -s https://centinelafinanciero.lat/ | grep -o 'Datos al [^<]*'
```

Y a ojo: que la fila esté, que la fecha haya cambiado y que el enlace de la fuente lleve a donde debe.

### 6. Revisar el scheduler

```bash
docker compose exec -T api python -m cli config list --grupo scheduler
docker compose exec -T db psql -U centinela -d centinela \
  -c "SELECT job, estado, iniciado_en FROM job_runs ORDER BY id DESC LIMIT 10;"
```

Si el recomputo de banderas lleva días sin correr, algo se apagó y nadie se enteró.

### 7. Copia del respaldo fuera del VPS

```bash
scp <usuario>@<vps>:~/centinela-financiero/backups/centinela-*.sql.gz ~/respaldos-centinela/
```

Una vez al mes, probar que restaura:

```bash
scripts/restaurar.sh ~/respaldos-centinela/<el-más-reciente>.sql.gz
```

---

## Un producto que cambió de forma

Si la institución cambió sus plazos, o dejó de ofrecer un producto, o sacó uno nuevo, el arreglo no está en `tasas.csv` sino en `seeds/productos.yaml`, y luego:

```bash
python -m cli seed          # idempotente: hace upsert por clave natural
```

Si una institución desaparece del mercado, `activa: false` en `instituciones.yaml`. Nunca se borra: sus tasas históricas siguen siendo observaciones válidas de cuando existía.

---

## Cambiar una URL sin cambiar la tasa

No hay camino directo, y es deliberado: `tasas` es append-only y la clave natural incluye la fecha, así que corregir sólo `fuente_url` de una observación ya cargada exige un `UPDATE` a mano.

```bash
docker compose exec -T db psql -U centinela -d centinela \
  -c "UPDATE tasas SET fuente_url = '<nueva>' WHERE id = <id>;"
```

Si pasa a menudo, es señal de que hace falta un comando para ello. Hasta ahora ha pasado una vez.
