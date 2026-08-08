# Centinela Financiero

[![test](https://github.com/gibran-ojeda/centinela-financiero/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/gibran-ojeda/centinela-financiero/actions/workflows/test.yml)

Comparador informativo de instrumentos de ahorro en México: CETES, SOFIPOs y
bancos digitales con **tasa efectiva neta** después de ISR, **cobertura de
seguro de depósito** y **banderas de salud institucional**.

**En producción: <https://centinelafinanciero.lat>**

No es asesoría de inversión, no recibe dinero, no intermedia contrataciones y
no cobra a las instituciones listadas. Cada tasa se muestra con su fecha y su
fuente.

## Qué resuelve

Una tasa anunciada no es lo que acabas ganando. Entre el número del anuncio y
tu bolsillo están la retención de ISR, la inflación y —si la institución
quiebra— el límite del fondo de protección. Centinela hace visibles esos tres
descuentos y el riesgo del emisor, con los mismos datos que publican Banxico y
la CNBV.

## Estado del proyecto

MVP en producción con las ingestas automatizadas:

- **Banxico SIE** a diario: CETES, TIIE, UDI, INPC.
- **Boletines CNBV** al mes: indicadores de salud (IMOR, ICAP, NICAP…) que
  alimentan las banderas.
- **Lectura de tasas** a la semana: fetch dirigido de las páginas oficiales
  (Chromium para las que renderizan por JavaScript) con extracción por LLM, y
  un researcher de búsqueda abierta para descubrimiento. Nada de lo que toca
  un LLM se publica solo: la primera lectura y cualquier cambio fuera de
  tolerancia pasan por una cola de revisión humana.

Política de transición del catálogo: las lecturas provenientes de agregadores
se publican **etiquetadas «sin verificar»** hasta que la lectura oficial de
cada producto las sustituye.

## Arquitectura

Cinco servicios en un solo compose, imagen Python única para los tres del
backend:

| Servicio | Qué hace |
|---|---|
| `web` | Astro SSR. El único servicio que ve el navegador (patrón BFF) |
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
docker compose exec api python -m cli seed
docker compose exec api python -m cli tasas import seeds/tasas.csv
```

El sitio queda en `http://127.0.0.1:8011` y la API en `http://127.0.0.1:8010`
— `/docs` para el contrato completo.

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

## Fuentes de datos

La regla de origen del proyecto: un número publicado sale de **quien lo ofrece
o de quien lo regula** — nunca de otro comparador.

- **[SIE de Banxico](https://www.banxico.org.mx/SieAPIRest/service/v1/)** —
  deuda gubernamental, TIIE, UDI e inflación (token gratuito).
- **[Portafolio de Información de la CNBV](https://portafolioinfo.cnbv.gob.mx/)**
  — indicadores mensuales de salud institucional.
- **Páginas oficiales de cada institución** — tasas de SOFIPOs y bancos
  digitales, leídas de la fuente y enlazadas desde cada dato del sitio.

Los datos pertenecen a sus fuentes; este proyecto los recopila, calcula sobre
ellos y los atribuye.

## Contribuir

Las guías están en [CONTRIBUTING.md](CONTRIBUTING.md); la convivencia, en el
[Código de Conducta](CODE_OF_CONDUCT.md); las vulnerabilidades, por el canal
privado de [SECURITY.md](SECURITY.md). En corto: el código habla español, los
títulos de commit van en inglés, y los datos tienen reglas que ningún PR puede
romper.

## Documentación

- `foundation-comparador-financiero-mx.md` — el documento de producto: qué se
  compara, cómo se calcula cada métrica y qué reglas rigen las banderas
- `docs/` — despliegue, runbook operativo y criterios de redacción de lo que
  el sitio afirma

## Licencia

MIT — ver [LICENSE](LICENSE). © 2026 Gibran Ojeda
