# Contribuir a Centinela Financiero

Gracias por el interés. Este documento cubre lo práctico: cómo levantar el
entorno, qué revisa la máquina antes que un humano, y las reglas del proyecto
que un PR no puede romper. Al participar aceptas el
[Código de Conducta](CODE_OF_CONDUCT.md).

## Setup local

```bash
git clone https://github.com/gibran-ojeda/brujula-financiera.git
cd brujula-financiera
cp .env.example .env        # y pon una POSTGRES_PASSWORD
pip install -e ".[app,dev]"
pre-commit install
docker compose up -d --build
docker compose exec api python -m cli seed
docker compose exec api python -m cli tasas import seeds/tasas.csv
```

El sitio queda en `http://127.0.0.1:8011` y la API en `http://127.0.0.1:8010`
(`/docs` para el contrato).

`pre-commit install` es el paso fácil de saltarse: sin él los hooks sólo
corren cuando se les llama a mano, y mypy —que en CI es informativo por
doctrina— se queda sin ninguna red. Y córrelo **desde el entorno en el que
commiteas**: el hook que genera apunta al intérprete que lo instaló, así que
uno instalado desde WSL aborta los commits hechos desde Windows (PyCharm, Git
Bash) con un `pre-commit not found`. Si trabajas partido —herramientas en WSL,
git en Windows— instala `pre-commit` también en el Python de ese lado: el hook
lo busca en el `PATH` como segundo camino y entonces sirve a los dos.

## Correr la suite

```bash
pytest
```

- El `.env` es obligatorio incluso para los tests: los settings se instancian
  al importar.
- Los tests que necesitan infraestructura real usan testcontainers y **se
  saltan solos** si no hay daemon de Docker, así que `pytest` pasa en una
  máquina sin el stack. Con Docker, la suite completa tarda ~9 minutos.
- `pre-commit run --all-files` corre lo mismo que revisará el hook en cada
  commit: ruff, black, mypy y los checks generales.

## Estilo

- **El código habla español**: identificadores, comentarios y docstrings. La
  única excepción son los nombres de los tests, que van en inglés
  (`test_the_job_is_registered_...`); su contenido y comentarios, en español.
- `black` y `ruff` con `line-length = 99`; los dos son bloqueantes en CI.
- `mypy --strict` corre en el hook de pre-commit; en CI es **informativo por
  doctrina** ([§18 del foundation](foundation-comparador-financiero-mx.md)):
  el listón está en el commit, no en el merge. No propongas volverlo
  bloqueante en un PR — es una decisión de diseño, no un descuido.

## Commits

Conventional Commits con el título **en inglés**, consistente con el
historial:

```
feat(rates): read the JavaScript sources from the VPS by default
fix(cli): let mypy see that a superseded row has a winner
docs(deploy): record the browser-in-VPS decision as applied
```

Un cambio lógico por commit.

## Proceso de PR

1. Para cambios grandes (módulo nuevo, cambio de arquitectura, nueva fuente de
   datos), abre un issue antes de escribir código.
2. Un PR = un cambio lógico. Describe qué cambia y por qué.
3. Si tocas variables de entorno, actualiza `.env.example` en el mismo PR — y
   si la variable es de producción, los **tres sitios**: el secreto/variable
   en GitHub, la plantilla de `scripts/lib/entorno.sh` —eligiendo su clase— y
   el mapa `environment:` del compose ([docs/despliegue.md](docs/despliegue.md)
   explica por qué, y qué significa cada clase).
4. Si cambias el comportamiento de un módulo, actualiza la documentación
   relevante en `docs/`.

## El dato manda

Las reglas de datos del proyecto no son negociables en un PR; conocerlas
ahorra iteraciones:

- **`tasas` es append-only.** Un dato no se «corrige» ni se borra: se observa
  de nuevo. La vigente de un producto la resuelve una ventana de vigencia al
  leer, no un UPDATE al escribir.
- **Toda tasa publicada conserva fuente y fecha** (`fuente_url`,
  `fecha_dato`), y la fuente es la institución o la autoridad — nunca un
  agregador. `AGREGADOR` existe solo como contraste y **jamás** puede quedar
  `VIGENTE` (invariante en el punto de escritura).
- **La primera lectura de un producto pasa por revisión humana**
  (`revisiones_tasas`, `python -m cli revisiones`), igual que cualquier cambio
  fuera de tolerancia. Coincidir con un dato sin verificar no lo verifica.

## Seguridad

Vulnerabilidades por el canal privado de [SECURITY.md](SECURITY.md), no por
issue público. Dudas generales: **contacto@centinelafinanciero.lat**.
