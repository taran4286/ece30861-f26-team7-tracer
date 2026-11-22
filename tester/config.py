"""The tester's settings, read from environment variables (or a git-ignored .env).

Separate from app/config.py, which the tester can't import.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator

from tester.model_coverage import system_identity

DEFAULT_OWN_SYSTEM_URL = "https://ece30861-f26-team7-tracer.vercel.app/system"


class TesterSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # The tester role's connection; it reaches only the tester's own table.
    tester_database_url: str
    # This is team identity for coverage, never inferred from a caller's Host header.
    tester_own_system_url: str = DEFAULT_OWN_SYSTEM_URL

    @field_validator("tester_own_system_url")
    @classmethod
    def valid_own_system_url(cls, value: str) -> str:
        system_identity(value)
        return value.rstrip("/")


@lru_cache
def get_tester_settings() -> TesterSettings:
    return TesterSettings()
