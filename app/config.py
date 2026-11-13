from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    jwt_secret_key: str
    jwt_issuer: str
    jwt_audience: str
    studio_allowed_user_ids: str = ""
    cors_allowed_origins: str = ""
    studio_data_dir: Path = Path("/tmp/studio")
    studio_ttl_hours: float = 24
    studio_max_file_gb: float = 3
    studio_max_project_gb: float = 10
    studio_render_timeout_min: float = 30
    studio_queue_max: int = 3

    @field_validator("jwt_secret_key")
    @classmethod
    def _secret_long_enough(cls, value: str) -> str:
        if len(value) < 32:
            raise ValueError("JWT_SECRET_KEY must be at least 32 characters")
        return value

    @field_validator("jwt_issuer")
    @classmethod
    def _issuer_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("JWT_ISSUER must not be empty")
        return value

    @field_validator("jwt_audience")
    @classmethod
    def _audience_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("JWT_AUDIENCE must not be empty")
        return value

    @property
    def allowed_user_ids(self) -> frozenset[str]:
        return frozenset(p.strip() for p in self.studio_allowed_user_ids.split(",") if p.strip())

    @property
    def cors_origins(self) -> list[str]:
        return [p.strip() for p in self.cors_allowed_origins.split(",") if p.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
