"""The tester's settings, read from environment variables (or a git-ignored .env).

Separate from app/config.py, which the tester can't import.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class TesterSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # The tester role's connection; it reaches only the tester's own table.
    tester_database_url: str


@lru_cache
def get_tester_settings() -> TesterSettings:
    return TesterSettings()
