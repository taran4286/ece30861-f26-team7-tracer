"""Settings read from environment variables (or a git-ignored .env)."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    # Drafting (services/llm.py). All optional: unset, no key and no model name are sent.
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    # The key goes only to a session llm_base_url with this URL's origin.
    llm_trusted_base_url: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
