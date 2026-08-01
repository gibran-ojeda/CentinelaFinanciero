"""Capa 1 de configuración: infraestructura y secretos desde el entorno.

Este módulo cubre lo que **no** puede cambiar sin reiniciar el proceso:
credenciales, URLs de servicios, puertos y flags de registro de jobs.

La capa 2 (parámetros de negocio ajustables en caliente: umbrales de banderas,
tolerancias de revisión, TTLs) vive en `core.config_store` y se expone con el
proxy `effective`, que respeta estos mismos nombres de atributo. Migrar un
consumidor de una capa a la otra es cambiar `settings.x` por `effective.x`.
"""

from __future__ import annotations

from decimal import Decimal
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
    app_name: str = "centinela-financiero"

    # ─── PostgreSQL ───────────────────────────────────────────
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5433
    postgres_db: str = "centinela"
    postgres_user: str = "centinela"
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
    scheduler_banderas_enabled: bool = True
    scheduler_tasas_enabled: bool = True
    scheduler_banxico_enabled: bool = True
    scheduler_cnbv_enabled: bool = True
    scheduler_frescura_enabled: bool = True
    #: Nivel 3. Arranca **apagado**: la búsqueda abierta es el camino más caro
    #: y menos preciso, y sólo debe encenderse cuando el nivel 2 lleva semanas
    #: corriendo y se sabe qué instituciones deja fuera de verdad.
    scheduler_research_enabled: bool = False
    scheduler_lock_ttl_seconds: int = 300
    scheduler_timezone: str = "America/Mexico_City"

    # ─── Parámetros de negocio (defaults) ─────────────────────
    # Estos SÍ son ajustables en caliente: el ConfigStore (capa 2) los
    # sobrescribe desde la base y `effective.x` devuelve el override si
    # existe, o el valor de aquí si no. Los defaults son los del foundation.
    umbral_imor_amarilla: Decimal = Decimal("3.0")
    umbral_imor_roja: Decimal = Decimal("6.0")
    umbral_icap_amarilla: Decimal = Decimal("15.0")
    umbral_icap_roja: Decimal = Decimal("10.5")
    umbral_cobertura_amarilla: Decimal = Decimal("100.0")
    umbral_cobertura_roja: Decimal = Decimal("70.0")
    umbral_gat_inconsistencia_pp: Decimal = Decimal("1.5")
    umbral_crecimiento_captacion_pct: Decimal = Decimal("50.0")
    umbral_tasa_sobre_mercado_pp: Decimal = Decimal("3.0")
    umbral_apalancamiento_amarilla: Decimal = Decimal("10.0")

    # Respaldo de último recurso, sólo si `valores_serie` está vacía. La
    # cobertura en MXN y la calculadora necesitan estos dos números desde la
    # fase 4; sin ellos no habría de dónde sacarlos. Los valores son reales
    # (SIE de Banxico, 2026-07-25) para que el respaldo no distorsione:
    # UDI del día e inflación INPC anual de junio 2026 contra junio 2025.
    udi_valor_fallback: Decimal = Decimal("8.791497")
    inflacion_anual_fallback: Decimal = Decimal("3.37")

    tolerancia_revision_pp: Decimal = Decimal("0.5")

    #: Publica también las tasas que siguen en PENDIENTE_REVISION, siempre
    #: marcadas «sin verificar» en su procedencia.
    #:
    #: Es la política de transición del lanzamiento: las lecturas de agregador
    #: se muestran etiquetadas hasta que la lectura oficial de cada producto
    #: las sustituya — la ventana de vigencia prefiere VIGENTE por estado, así
    #: que la sustitución es automática producto a producto. Cuando ya no
    #: quede nada sin verificar, la bandera deja de tener efecto; se conserva
    #: como kill-switch para poder ocultar lo no verificado sin deploy.
    mostrar_tasas_sin_verificar: bool = True

    cache_comparador_ttl_seconds: int = 300
    banderas_recompute_enabled: bool = True
    tasas_fetch_enabled: bool = True
    #: El job del VPS sólo lee lo que rinde sin navegador. En `true` mientras
    #: Chromium no esté en la imagen del scheduler: las fuentes que necesitan
    #: JavaScript se corren desde local con `cli tasas fetch --solo-navegador`.
    #: Ver la sección «Navegador en el VPS» de docs/despliegue.md.
    tasas_fetch_solo_sin_js: bool = True
    #: Kill-switch caliente de la ingesta de Banxico. Sin `BANXICO_TOKEN` el
    #: job se omite igualmente, así que esto es para apagarla teniéndolo.
    banxico_sync_enabled: bool = True
    #: Kill-switch caliente de la ingesta de la CNBV.
    cnbv_ingesta_enabled: bool = True
    #: Kill-switch caliente de la búsqueda abierta (nivel 3).
    tasas_research_enabled: bool = True
    config_cache_ttl_seconds: int = 60

    # ─── Fuentes de datos (fases 7 y 9) ───────────────────────
    banxico_token: SecretStr = SecretStr("")
    deepseek_api_key: SecretStr = SecretStr("")

    # ─── Banxico / SIE (fase 7) ───────────────────────────────
    banxico_timeout_seconds: float = 30.0
    #: Cuántas veces se reintenta un lote **además** del intento inicial, y sólo
    #: ante lo que el tiempo cura. Un token inválido no entra aquí: llega como
    #: 400 y se propaga en el primer intento.
    banxico_max_reintentos: int = 2

    # ─── CNBV / Portafolio de Información (fase 8) ────────────
    #: Los boletines pesan megabytes y el portal no va rápido. Más generoso
    #: que el resto de timeouts a propósito.
    cnbv_timeout_seconds: float = 180.0
    #: Dónde se guardan los archivos crudos descargados. Sin el original, un
    #: indicador de la CNBV no es auditable: es un número que alguien dice que
    #: leyó. En el VPS va a un volumen, no al sistema de archivos del contenedor.
    cnbv_directorio_descargas: str = "var/cnbv"

    # ─── LLM (extracción de tasas, fase 9) ────────────────────
    llm_base_url: str = "https://api.deepseek.com/v1"
    #: `deepseek-chat` y `deepseek-reasoner` los retiró DeepSeek el 2026-07-24.
    #: Se usa el económico: la extracción es leer una tabla, no razonar. Un
    #: razonador además gasta el presupuesto de tokens pensando y devuelve el
    #: contenido vacío (visto en NarrativeAlpha).
    llm_modelo_extraccion: str = "deepseek-v4-flash"
    llm_timeout_seconds: float = 90.0
    #: Techo duro de gasto diario (D2). Red contra bucles, no presupuesto: lo
    #: esperado son centavos por semana.
    llm_cost_daily_limit_usd: float = 1.0

    # ─── Descarga de páginas de tasas (fase 9) ────────────────
    #: El bot se identifica y da dónde reclamar. No se imita un navegador para
    #: esquivar un WAF: si una institución bloquea a un bot identificado, esa
    #: fuente pasa a lectura manual.
    fetch_user_agent: str = (
        "Mozilla/5.0 (compatible; CentinelaFinancieroBot/1.0; "
        "+https://centinelafinanciero.lat/aviso-legal)"
    )
    fetch_timeout_seconds: float = 20.0
    #: Reintentos **además** del intento inicial, y sólo ante errores que el
    #: tiempo puede curar.
    fetch_max_reintentos: int = 1
    #: Errores duros por host antes de dejarlo para la siguiente corrida.
    fetch_umbral_circuito: int = 2
    #: Backoff temporal ante una cadena degradada por algo transitorio. Los
    #: valores vienen de NarrativeAlpha, donde están calibrados en producción:
    #: cinco minutos y luego veinte. No reducir sin medir.
    fetch_esperas_backoff_s: list[float] = [300.0, 1200.0]
    #: Menos texto que esto es una página que no se pudo leer, no una sin tasas.
    fetch_min_caracteres: int = 200
    fetch_respetar_robots: bool = True

    # ─── Búsqueda abierta (fase 9, nivel 3) ───────────────────
    #: Cadena de motores, en orden. Se recorren hasta que uno devuelva algo.
    #: `ddgs` es un metabuscador: estos son sus backends, no servicios propios.
    #: **Aquí es donde se cambia de proveedor sin tocar código** — si la
    #: calibración pide el SearXNG del VPS, se declara en esta variable.
    research_motores: str = "duckduckgo,google,brave"
    research_max_reintentos: int = 1
    #: Cuántas vueltas de tool-use antes de retirar las herramientas y exigir
    #: el JSON final. Sin tope, un modelo que no converge gasta el presupuesto
    #: del día buscando.
    research_max_rondas: int = 4
    #: Resultados por búsqueda. Más contexto es más tokens por vuelta.
    research_resultados_por_busqueda: int = 6

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
