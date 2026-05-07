"""Application configuration loaded from environment variables."""

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Lenny backend configuration.

    All values can be overridden via environment variables.
    """

    DATABASE_URL: str = "postgresql+asyncpg://mct2:mct2@db:5432/mct2"
    MEASURE_ENGINE_URL: str = "http://hapi-fhir-measure:8080/fhir"
    DEFAULT_CDR_URL: str = "http://hapi-fhir-cdr:8080/fhir"
    BATCH_SIZE: int = 100
    MAX_WORKERS: int = 4
    MAX_RETRIES: int = 3
    PATIENT_DATA_STRATEGY: str = "batch"
    VALUESET_RELOAD_MODE: str = "delete"
    LOG_LEVEL: str = "INFO"
    ALLOWED_ORIGINS: str = "*"  # Comma-separated origins, or "*" for all (local dev default)

    model_config = {"env_prefix": "", "case_sensitive": True}

    @field_validator("PATIENT_DATA_STRATEGY")
    @classmethod
    def validate_patient_data_strategy(cls, value: str) -> str:
        allowed = {"batch", "data_requirements"}
        if value not in allowed:
            raise ValueError(f"PATIENT_DATA_STRATEGY must be one of: {', '.join(sorted(allowed))}")
        return value

    @field_validator("VALUESET_RELOAD_MODE")
    @classmethod
    def validate_valueset_reload_mode(cls, value: str) -> str:
        allowed = {"delete", "remap"}
        if value not in allowed:
            raise ValueError(f"VALUESET_RELOAD_MODE must be one of: {', '.join(sorted(allowed))}")
        return value


def parse_allowed_origins(raw: str) -> list[str]:
    """Parse ALLOWED_ORIGINS env var into a list for CORSMiddleware.

    Returns ["*"] for wildcard. An empty or whitespace-only string returns []
    (all cross-origin requests blocked). Otherwise splits on commas and strips
    whitespace from each entry.
    """
    if raw == "*":
        return ["*"]
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


settings = Settings()

MAX_UPLOAD_SIZE = 100 * 1024 * 1024
