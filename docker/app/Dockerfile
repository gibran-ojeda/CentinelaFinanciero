# Imagen única `centinela:latest`. Los servicios (api, scheduler, cli) se
# diferencian sólo por el `command` del compose — build una vez, deploy N.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# gcc y libpq-dev por si asyncpg cae al build desde fuente; curl para los
# healthchecks del compose.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --upgrade pip && pip install ".[app]"

# Las migraciones viajan en la imagen: el deploy corre `alembic upgrade head`
# dentro del contenedor, contra el mismo código que va a servir.
COPY alembic.ini ./
COPY alembic/ ./alembic/

# Los catálogos semilla también. El ciclo semanal del runbook corre
# `python -m cli seed` y `python -m cli tasas import` **dentro** del
# contenedor: sin esto, ahí no hay nada que importar. Son YAML y CSV.
COPY seeds/ ./seeds/

# Usuario sin privilegios: nada aquí necesita root en runtime.
RUN useradd --create-home --uid 10001 centinela && chown -R centinela:centinela /app
USER centinela

CMD ["python", "-m", "api"]
