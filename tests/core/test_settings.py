"""Tests de la capa de configuración."""

from __future__ import annotations

from pydantic import SecretStr

from core.settings import Settings, settings


def test_singleton_uses_test_environment() -> None:
    assert settings.environment == "test"
    assert settings.is_production is False


def test_database_url_is_async_dsn() -> None:
    s = Settings(
        postgres_host="db",
        postgres_port=5433,
        postgres_db="centinela",
        postgres_user="centinela",
        postgres_password=SecretStr("s3cr3t"),
    )
    assert s.database_url == "postgresql+asyncpg://centinela:s3cr3t@db:5433/centinela"


def test_redis_url_omits_auth_when_no_password() -> None:
    s = Settings(redis_host="redis", redis_port=6380, redis_db=2, redis_password=SecretStr(""))
    assert s.redis_url == "redis://redis:6380/2"


def test_redis_url_includes_auth_when_password_set() -> None:
    s = Settings(redis_host="redis", redis_port=6380, redis_db=0, redis_password=SecretStr("pw"))
    assert s.redis_url == "redis://:pw@redis:6380/0"


def test_secrets_are_not_leaked_by_repr_or_dump() -> None:
    """`database_url` es property y no computed_field justamente por esto.

    Si volviera a ser computed_field, la contraseña viajaría en el repr y en
    cualquier `model_dump()` que acabe en un log.
    """
    s = Settings(postgres_password=SecretStr("no-debe-aparecer"))
    assert "no-debe-aparecer" not in repr(s)
    assert "no-debe-aparecer" not in str(s.postgres_password)
    assert "no-debe-aparecer" not in str(s.model_dump())
    # Pero sigue siendo accesible de forma explícita para quien la necesita.
    assert "no-debe-aparecer" in s.database_url


def test_log_format_defaults_by_environment() -> None:
    assert Settings(environment="prod").effective_log_format == "json"
    assert Settings(environment="dev").effective_log_format == "pretty"
    # Un override explícito gana sobre la heurística.
    assert Settings(environment="prod", log_format="pretty").effective_log_format == "pretty"


def test_empty_log_format_means_unset() -> None:
    """Un `.env` no distingue entre variable ausente y puesta a vacío.

    `LOG_FORMAT=` es la forma documentada de decir "sin override"; sin esta
    normalización el proceso no arranca dentro de Docker.
    """
    assert Settings(environment="prod", log_format="").effective_log_format == "json"
    assert Settings(environment="dev", log_format="   ").effective_log_format == "pretty"


def test_sensitive_patterns_are_normalized() -> None:
    s = Settings(log_sensitive_patterns=" Password , TOKEN ,, api_key ")
    assert s.sensitive_patterns == ("password", "token", "api_key")


def test_cors_origins_are_split_and_stripped() -> None:
    s = Settings(api_cors_origins="https://a.mx, https://b.mx ")
    assert s.cors_origins == ("https://a.mx", "https://b.mx")
    assert Settings(api_cors_origins="").cors_origins == ()
