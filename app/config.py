"""Settings read from environment variables (or a git-ignored .env)."""

from enum import StrEnum
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class RegistrantType(StrEnum):
    DOMESTIC = "Domestic"
    FPI = "FPI"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    registrant_type: RegistrantType = RegistrantType.DOMESTIC


@lru_cache
def get_settings() -> Settings:
    return Settings()
