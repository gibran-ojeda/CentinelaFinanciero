"""Configuración global de pytest.

`core.settings` instancia el singleton `settings` **al importarse**, así que las
variables de entorno tienen que estar puestas antes de que cualquier test (o
cualquier módulo que ellos importen) toque `core`. Por eso esto vive arriba del
archivo y no en una fixture.
"""

from __future__ import annotations

import os

_TEST_ENV: dict[str, str] = {
    "ENVIRONMENT": "test",
    "POSTGRES_HOST": "127.0.0.1",
    "POSTGRES_PORT": "5433",
    "POSTGRES_DB": "brujula_test",
    "POSTGRES_USER": "brujula",
    "POSTGRES_PASSWORD": "test-password",
    "REDIS_HOST": "127.0.0.1",
    "REDIS_PORT": "6380",
    "REDIS_DB": "15",
    "LOG_LEVEL": "WARNING",
    "API_READ_KEY": "test-read-key",
    "API_ADMIN_KEY": "test-admin-key",
}

for _key, _value in _TEST_ENV.items():
    os.environ.setdefault(_key, _value)

# Nota de precedencia: pydantic-settings resuelve variables de entorno **antes**
# que el `.env`, así que estos valores ganan sobre el `.env` del desarrollador
# (y sobre el `cp .env.example .env` que hace el CI) sin necesidad de borrarlo.
