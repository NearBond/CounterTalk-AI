"""Settings, read only through get_settings() (CODING_GUIDELINES §1.7)."""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent

_PSYCOPG = "postgresql+psycopg://"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # A bad DATABASE_URL must not print its password in the error.
        hide_input_in_errors=True,
    )

    DATABASE_URL: str = "postgresql+psycopg://countertalk:countertalk@127.0.0.1:5432/countertalk"
    # Relative paths are inside backend/. Railway sets /data, its volume.
    DATA_DIR: Path = Path("data")
    DEBUG: bool = False
    # One Meta key for both Muse Voice Transcribe and Muse Spark.
    MODEL_API_KEY: SecretStr = SecretStr("")
    SECRET_KEY: SecretStr = SecretStr("")

    @field_validator("DATABASE_URL")
    @classmethod
    def _name_the_psycopg_driver(cls, url: str) -> str:
        # Railway gives postgres:// or postgresql://; SQLAlchemy needs the driver in the scheme.
        for scheme in ("postgres://", "postgresql://"):
            if url.startswith(scheme):
                return _PSYCOPG + url.removeprefix(scheme)
        if not url.startswith(_PSYCOPG):
            raise ValueError("DATABASE_URL must be a postgres:// or postgresql:// URL")
        return url

    @field_validator("DATA_DIR")
    @classmethod
    def _inside_backend_if_relative(cls, path: Path) -> Path:
        return path if path.is_absolute() else BACKEND_DIR / path


@lru_cache
def get_settings() -> Settings:
    return Settings()
