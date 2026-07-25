# Fase 1 — Scaffold e infraestructura

## Objetivo

Repo ejecutable de punta a punta **sin lógica de negocio**: `docker compose up` levanta db, redis, una API hello-world y un scheduler con un job vacío. Toda la fontanería (config, logging, conexiones, locks, CI) queda resuelta aquí para que las fases siguientes solo añadan dominio.

## Entregables

- `pyproject.toml` único: proyecto `brujula-financiera`, Python `>=3.12`, layout `src/` con paquetes planos, extras `[api]`, `[scheduler]`, `[ingest]`, `[llm]`, `[browser]`, `[mcp]`, `[dev]`; configuración de ruff, black, mypy strict y pytest (`asyncio_mode = "auto"`) en el mismo archivo.
- `src/core/`:
  - `settings.py` — `Settings(BaseSettings)` con `env_file=".env"`, `SecretStr` para secretos, singleton `settings`.
  - `logging.py` — structlog: JSON en prod, pretty en dev; processor que redacta claves sensibles.
  - `db.py` — `create_async_engine` (pool_pre_ping, pool_recycle) + `async_sessionmaker`.
  - `redis.py` — cliente redis async con reconexión y degradación (si Redis cae, la app sigue).
- `src/api/` — `app.py` con factory `create_app()`, `__main__.py` (uvicorn), router `GET /healthz` que reporta estado de db y redis.
- `src/scheduler/` — `runner.py` (`AsyncIOScheduler`, `job_defaults={"misfire_grace_time": 60, "coalesce": True, "max_instances": 1}`, shutdown por SIGINT/SIGTERM), `registry.py` (lista declarativa `JOBS_REGISTRY` de dicts `{id, func, trigger, name}` con registro condicional por flag), `locks.py` (lock distribuido Redis `SET NX` + TTL + liberación compare-and-delete vía Lua), `__main__.py`, y un job noop `heartbeat` que loguea y escribe en la (futura) tabla `job_runs` — por ahora solo log.
- `docker/app/Dockerfile` — `python:3.12-slim`, imagen única `brujula:latest`; servicios diferenciados por `command`.
- `docker-compose.yml` — servicios `db` (postgres:16, healthcheck `pg_isready`, volumen nombrado), `redis` (redis:7-alpine, `--appendonly yes`), `api` (`python -m api`, puerto 8000 en loopback), `scheduler` (`python -m scheduler`). Todos con `restart: unless-stopped`, healthchecks y logging `json-file` rotado.
- `.env.example` documentado por secciones (Entorno, PostgreSQL, Redis, Logging, API, Scheduler) — se mantiene al día en cada fase que añada variables.
- `.pre-commit-config.yaml` — ruff (`--fix`), black, mypy (`files: ^src/`).
- `.github/workflows/test.yml` — jobs `lint` (ruff, bloqueante), `typecheck` (mypy, `continue-on-error: true`), `test` (pytest tras `cp .env.example .env`, bloqueante).
- `tests/` — `conftest.py` que setea env vars requeridas **antes** de los imports (settings se instancia al importar), tests de humo de `core` y del `/healthz`.

## Tareas

1. Inicializar `pyproject.toml` con dependencias base: `pydantic>=2`, `pydantic-settings`, `structlog`, `sqlalchemy[asyncio]>=2.0`, `asyncpg`, `redis[hiredis]`, `apscheduler>=3.10,<4`, `fastapi`, `uvicorn[standard]`, `httpx`, `python-dotenv`, `alembic`; `[dev]`: `pytest`, `pytest-asyncio`, `pytest-cov`, `respx`, `testcontainers[postgres,redis]`, `ruff`, `black`, `mypy`.
2. Implementar `src/core/` en el orden settings → logging → db → redis.
3. Implementar la factory FastAPI y `/healthz` (ping a db con `SELECT 1` y a redis con `PING`; responde 200 con detalle por dependencia, 503 si db cae).
4. Implementar el esqueleto del scheduler con el job `heartbeat` (IntervalTrigger 60s) tomando lock Redis; verificar que dos instancias del scheduler no ejecutan el job a la vez.
5. Escribir Dockerfile y compose; probar ciclo completo `docker compose up --build`.
6. Configurar pre-commit y CI; abrir el primer PR y verificar el pipeline.

## Criterios de aceptación

- [ ] `docker compose up --build` levanta los 4 servicios con healthchecks verdes.
- [ ] `curl http://127.0.0.1:8000/healthz` responde 200 con estado de db y redis.
- [ ] Los logs del scheduler muestran `heartbeat` ejecutándose cada 60s con lock adquirido/liberado.
- [ ] Con dos réplicas del scheduler corriendo, `heartbeat` se ejecuta una sola vez por intervalo (lock funciona).
- [ ] `pre-commit run --all-files` pasa; CI verde en el PR.
- [ ] `pytest` pasa en local sin Docker levantado (los tests de humo no requieren infra o usan testcontainers).

## Dependencias

Ninguna. Primera fase.
