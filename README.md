# Centinela Financiero

Comparador informativo de instrumentos de ahorro en México: CETES, SOFIPOs y
bancos digitales con **tasa efectiva neta** después de ISR, **cobertura de
seguro de depósito** y **banderas de salud institucional**.

No es asesoría de inversión, no recibe dinero, no intermedia contrataciones y
no cobra a las instituciones listadas. Cada tasa se muestra con su fecha y su
fuente.

## Qué resuelve

Una tasa anunciada no es lo que acabas ganando. Entre el número del anuncio y
tu bolsillo están la retención de ISR, la inflación y —si la institución
quiebra— el límite del fondo de protección. Centinela hace visibles esos tres
descuentos y el riesgo del emisor, con los mismos datos que publican Banxico y
la CNBV.

## Arquitectura

Cuatro servicios en un solo compose, imagen Python única para los tres del
backend:

| Servicio | Qué hace |
|---|---|
| `api` | FastAPI interna. Comparador, detalle de institución, calculadora. Nunca se expone a internet |
| `scheduler` | APScheduler con lock distribuido en Redis. Ingesta y recomputo de banderas |
| `db` | PostgreSQL 16. Esquema versionado con Alembic |
| `redis` | Cache L2, locks distribuidos y guarda de disparos del scheduler |

`Decimal` de punta a punta para dinero y tasas. El motor de métricas
(`src/metrics/`) son funciones puras sin infraestructura: los umbrales entran
como argumento, nunca importando configuración.

## Arranque

```bash
cp .env.example .env   # y pon una POSTGRES_PASSWORD
docker compose up -d --build
python -m cli seed
python -m cli tasas import seeds/tasas.csv
```

La API queda en `http://127.0.0.1:8010` — `/docs` para el contrato completo.

## Desarrollo

```bash
pip install -e ".[app,dev]"
pre-commit install
pytest
pre-commit run --all-files
```

`pre-commit install` es el paso fácil de saltarse: sin él los hooks sólo corren
cuando se les llama a mano, y mypy —que en CI es informativo por doctrina— se
queda sin ninguna red.

Córrelo **desde el entorno en el que commiteas**. El hook que genera apunta al
intérprete que lo instaló, así que uno instalado desde WSL aborta los commits
hechos desde Windows (PyCharm, Git Bash) con un `pre-commit not found`. Si
trabajas partido —herramientas en WSL, git en Windows— instala `pre-commit`
también en el Python de ese lado: el hook lo busca en el `PATH` como segundo
camino y entonces sirve a los dos.

Los tests que necesitan infraestructura real usan testcontainers y se **saltan**
si no hay un daemon de Docker, así que `pytest` pasa en una máquina sin el stack
levantado.

## Documentación

- `foundation-comparador-financiero-mx.md` — el documento de producto: qué se
  compara, cómo se calcula cada métrica y qué reglas rigen las banderas
- `plan-de-implementacion/` — las diez fases, con entregables y criterios de
  aceptación por fase

## Licencia

MIT
