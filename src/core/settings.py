"""Capa 1 de configuración: infraestructura y secretos desde el entorno.

Este módulo cubre lo que **no** puede cambiar sin reiniciar el proceso:
credenciales, URLs de servicios, puertos y flags de registro de jobs.

La capa 2 (parámetros de negocio ajustables en caliente: umbrales de banderas,
tolerancias de revisión, TTLs) vive en `core.config_store` y se expone con el
proxy `effective`, que respeta estos mismos nombres de atributo. Migrar un
consumidor de una capa a la otra es cambiar `settings.x` por `effective.x`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["dev", "prod", "test"]
LogFormat = Literal["json", "pretty"]


class Settings(BaseSettings):
    """Configuración del proceso, cargada de variables de entorno y `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ─── Entorno ──────────────────────────────────────────────
    environment: Environment = "dev"
    app_name: str = "brujula-financiera"

    # ─── PostgreSQL ───────────────────────────────────────────
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5433
    postgres_db: str = "brujula"
    postgres_user: str = "brujula"
    postgres_password: SecretStr = SecretStr("")
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_pool_recycle_seconds: int = 1800
    db_echo: bool = False

    # ─── Redis ────────────────────────────────────────────────
    redis_host: str = "127.0.0.1"
    redis_port: int = 6380
    redis_db: int = 0
    redis_password: SecretStr = SecretStr("")
    redis_socket_timeout_seconds: float = 3.0

    # ─── Logging ──────────────────────────────────────────────
    log_level: str = "INFO"
    log_format: LogFormat | None = None
    log_sensitive_patterns: str = "password,secret,token,api_key,apikey,authorization"

    # ─── API ──────────────────────────────────────────────────
    api_host: str = "0.0.0.0"  # noqa: S104 — publicado sólo en loopback vía compose
    api_port: int = 8000
    api_read_key: SecretStr = SecretStr("")
    api_admin_key: SecretStr = SecretStr("")
    api_cors_origins: str = ""

    # ─── Scheduler ────────────────────────────────────────────
    # Gate frío (env-only): decide si el job llega a registrarse. El gate
    # caliente (kill-switch sin reinicio) vive en ConfigStore — ver §13
    # del foundation, patrón de doble gate.
    scheduler_heartbeat_enabled: bool = True
    scheduler_heartbeat_interval_seconds: int = 60
    scheduler_lock_ttl_seconds: int = 300
    scheduler_timezone: str = "America/Mexico_City"

    # ─── Fuentes de datos (fases 7 y 9) ───────────────────────
    banxico_token: SecretStr = SecretStr("")
    deepseek_api_key: SecretStr = SecretStr("")

    @field_validator("log_format", mode="before")
    @classmethod
    def _empty_string_is_unset(cls, value: Any) -> Any:
        """`LOG_FORMAT=` en el `.env` significa "sin override", no cadena vacía.

        Un archivo `.env` no distingue entre una variable ausente y una puesta
        a vacío: ambas llegan aquí, pero `""` no valida contra el `Literal`.
        Documentamos la variable como opcional, así que la normalizamos.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    # Nota: estas URLs son `property` y NO `computed_field` a propósito. Un
    # computed_field entra en el repr y en `model_dump()`, lo que filtraría la
    # contraseña en claro en cualquier log o traza que serialice los settings.
    @property
    def database_url(self) -> str:
        """DSN async para SQLAlchemy/asyncpg."""
        password = self.postgres_password.get_secret_value()
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        password = self.redis_password.get_secret_value()
        auth = f":{password}@" if password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def is_production(self) -> bool:
        return self.environment == "prod"

    @property
    def effective_log_format(self) -> LogFormat:
        """JSON en producción, legible en desarrollo, salvo override explícito."""
        if self.log_format is not None:
            return self.log_format
        return "json" if self.is_production else "pretty"

    @property
    def sensitive_patterns(self) -> tuple[str, ...]:
        return tuple(
            p.strip().lower() for p in self.log_sensitive_patterns.split(",") if p.strip()
        )

    @property
    def cors_origins(self) -> tuple[str, ...]:
        return tuple(o.strip() for o in self.api_cors_origins.split(",") if o.strip())


settings = Settings()
"""Singleton del proceso.

Se instancia al importar el módulo, así que los tests deben fijar las variables
de entorno **antes** de importar cualquier cosa de `core` (ver tests/conftest.py).
"""


__all__ = ["Environment", "LogFormat", "Settings", "settings"]
