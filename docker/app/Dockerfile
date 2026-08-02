# Imagen única `centinela:latest`. Los servicios (api, scheduler, cli) se
# diferencian sólo por el `command` del compose — build una vez, deploy N.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1
# Los navegadores de Playwright, fuera de /root/.cache: el runtime corre como
# uid 10001 y a /root no puede ni entrar. La leen el `playwright install` de
# abajo (escritor) y el transporte en runtime (lector) — por eso es ENV.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# gcc y libpq-dev por si asyncpg cae al build desde fuente; curl para los
# healthchecks del compose.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

# Chromium ANTES de copiar nada del repo: esta capa pesa ~450 MB y sólo debe
# reconstruirse cuando cambie esta línea — no en cada deploy de código, que
# es cuando el VPS hace build. `--with-deps` instala las librerías de sistema
# que python:3.12-slim no trae (root, aquí lo hay). El pin coincide con el
# extra [browser] de pyproject; `chmod a+rX` y no chown porque al runtime le
# basta leer y ejecutar.
RUN pip install --upgrade pip && pip install "playwright>=1.40" \
    && playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install ".[app]"

# Las migraciones viajan en la imagen: el deploy corre `alembic upgrade head`
# dentro del contenedor, contra el mismo código que va a servir.
COPY alembic.ini ./
COPY alembic/ ./alembic/

# Los catálogos semilla también. El ciclo semanal del runbook corre
# `python -m cli seed` y `python -m cli tasas import` **dentro** del
# contenedor: sin esto, ahí no hay nada que importar. Son YAML y CSV.
COPY seeds/ ./seeds/

# Y las plantillas de prompts, por la misma razón que los seeds: viven en la
# raíz del repo, fuera de `src/`, así que `pip install` no las lleva.
# `rates_agent/prompts.py` las busca en `Path.cwd()/"prompts"` (= /app/prompts);
# sin esta línea, el extractor del fetch L2 y el researcher L3 mueren con
# FileNotFoundError en la primera llamada de cada corrida.
COPY prompts/ ./prompts/

# El punto de montaje del volumen de boletines CNBV se crea antes del chown:
# un volumen nombrado hereda el dueño que el directorio tenga en la imagen, y
# si Docker lo crea al vuelo nace de root y el uid 10001 no puede escribir.
RUN mkdir -p /app/var/cnbv

# Usuario sin privilegios: nada aquí necesita root en runtime.
RUN useradd --create-home --uid 10001 centinela && chown -R centinela:centinela /app
USER centinela

CMD ["python", "-m", "api"]
