"""Application configuration loaded from environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """MCT2 backend configuration.

    All values can be overridden via environment variables.
    """

    DATABASE_URL: str = "postgresql+asyncpg://mct2:mct2@db:5432/mct2"
    MEASURE_ENGINE_URL: str = "http://hapi-fhir-measure:8080/fhir"
    DEFAULT_CDR_URL: str = "http://hapi-fhir-cdr:8080/fhir"
    BATCH_SIZE: int = 100
    MAX_WORKERS: int = 4
    MAX_RETRIES: int = 3
    LOG_LEVEL: str = "INFO"

    model_config = {"env_prefix": "", "case_sensitive": True}


settings = Settings()
