"""Settings, read only through get_settings() (CODING_GUIDELINES §1.7)."""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

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
    # The built web app (npm run build). Served only if it holds an index.html; in development
    # Vite serves the app instead. The Docker image sets its own path.
    WEB_DIST_DIR: Path = BACKEND_DIR.parent / "web" / "dist"
    DEBUG: bool = False
    # One Meta key for both Muse Voice Transcribe and Muse Spark.
    MODEL_API_KEY: SecretStr = SecretStr("")
    # Signs the session cookies (countertalk.auth). The API won't start without it unless DEBUG
    # is on (main.create_app); scripts and the worker don't need it.
    SECRET_KEY: SecretStr = SecretStr("")

    @field_validator("DATABASE_URL")
    @classmethod
    def _name_the_psycopg_driver(cls, url: str) -> str:
        # Railway gives postgres:// or postgresql://; SQLAlchemy needs the driver in the scheme.
        for scheme in ("postgres://", "postgresql://"):
            if url.startswith(scheme):
                url = _PSYCOPG + url.removeprefix(scheme)
                break
        if not url.startswith(_PSYCOPG):
            raise ValueError("DATABASE_URL must be a postgres:// or postgresql:// URL")
        # An unescaped "@" in the password makes the rest of it read as the host, which is then
        # printed in error lines. Ask the parser what it saw, and refuse rather than show any of
        # it.
        try:
            host = make_url(url).host or ""
        except ArgumentError:
            raise ValueError("DATABASE_URL isn't a URL the database driver can read") from None
        if "@" in host:
            raise ValueError("DATABASE_URL's password has an @: write it as %40")
        return url

    @field_validator("DATA_DIR")
    @classmethod
    def _data_dir(cls, path: Path) -> Path:
        return _inside_backend_if_relative(path)

    @field_validator("WEB_DIST_DIR")
    @classmethod
    def _web_dist_dir(cls, path: Path) -> Path:
        return _inside_backend_if_relative(path).resolve()


def _inside_backend_if_relative(path: Path) -> Path:
    return path if path.is_absolute() else BACKEND_DIR / path


@lru_cache
def get_settings() -> Settings:
    return Settings()


class Misconfigured(RuntimeError):
    """A setting the app can't run without."""


def secret_key() -> str:
    """SECRET_KEY, which signs the session cookies (batch 16). Empty with DEBUG off: the
    API won't start (main's lifespan), and nothing is signed with no key; with DEBUG on, ""
    (auth then signs with a key of its own process)."""
    settings = get_settings()
    key = settings.SECRET_KEY.get_secret_value()
    if not key and not settings.DEBUG:
        raise Misconfigured(
            "SECRET_KEY is empty: sign-in sessions are signed with it. Set it in the host's"
            " variables (or backend/.env), e.g. from"
            ' python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    return key
