## Qué cambia

Descripción breve del cambio.

## Por qué

Motivación / problema que resuelve (no repitas el diff, explica el porqué).

## Cómo se probó

- [ ] `pytest` pasa (los tests de testcontainers se saltan solos si no hay Docker)
- [ ] `ruff check src tests` y `black --check src tests` sin errores
- [ ] `pre-commit run --all-files` pasa — incluye mypy strict, que en CI es informativo por doctrina ([§18 del foundation](../foundation-comparador-financiero-mx.md))
- [ ] Probado manualmente (describe cómo, si aplica)

## Checklist

- [ ] Actualicé `.env.example` si agregué o renombré variables de entorno — y si la variable es de producción, los **tres sitios** (secreto/variable en GitHub → plantilla de `scripts/lib/entorno.sh`, con su clase → mapa `environment:` del compose, ver [docs/despliegue.md](../docs/despliegue.md))
- [ ] Actualicé la documentación relevante en `docs/` si cambié el comportamiento de un módulo
- [ ] El PR es de un solo cambio lógico
