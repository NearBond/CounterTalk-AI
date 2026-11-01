from pathlib import Path

import pytest
from pydantic import ValidationError

from countertalk.config import BACKEND_DIR, Settings, get_settings
from tests.conftest import PINNED_SETTINGS


def settings(**values: object) -> Settings:
    """Settings from these values only: no .env file."""
    return Settings(_env_file=None, **values)


@pytest.fixture
def no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Removes the pinned environment, which would otherwise win over .env files and defaults."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+psycopg"])
def test_database_url_scheme_becomes_psycopg(scheme: str) -> None:
    url = settings(DATABASE_URL=f"{scheme}://u:p@db.example.com:5432/app").DATABASE_URL

    assert url == "postgresql+psycopg://u:p@db.example.com:5432/app"


@pytest.mark.parametrize(
    "url", ["", "mysql://u:p@h/app", "postgresql+asyncpg://u:p@h/app", "sqlite:///app.db"]
)
def test_database_url_that_is_not_postgres_is_refused(url: str) -> None:
    with pytest.raises(ValidationError):
        settings(DATABASE_URL=url)


def test_database_url_error_does_not_show_the_password() -> None:
    with pytest.raises(ValidationError) as error:
        settings(DATABASE_URL="mysql://admin:hunter2-not-real@db.example.com/app")

    assert "hunter2-not-real" not in str(error.value)


def test_relative_data_dir_is_inside_backend() -> None:
    assert settings(DATA_DIR="data").DATA_DIR == BACKEND_DIR / "data"


def test_absolute_data_dir_is_kept(tmp_path: Path) -> None:
    assert settings(DATA_DIR=str(tmp_path)).DATA_DIR == tmp_path


def test_keys_are_hidden_when_settings_are_printed() -> None:
    values = settings(MODEL_API_KEY="model-key-not-real", SECRET_KEY="secret-key-not-real")

    assert "model-key-not-real" not in repr(values)
    assert "secret-key-not-real" not in str(values)
    assert values.MODEL_API_KEY.get_secret_value() == "model-key-not-real"


@pytest.mark.usefixtures("no_env")
def test_unknown_keys_in_env_file_are_ignored(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET_KEY=from-file\nSOMETHING_ELSE=1\n", encoding="utf-8")

    values = Settings(_env_file=env_file)

    assert values.SECRET_KEY.get_secret_value() == "from-file"


@pytest.mark.usefixtures("no_env")
def test_missing_settings_take_safe_defaults() -> None:
    values = settings()

    assert values.DATA_DIR == BACKEND_DIR / "data"
    assert values.WEB_DIST_DIR == BACKEND_DIR.parent / "web" / "dist"
    assert values.DEBUG is False
    assert values.MODEL_API_KEY.get_secret_value() == ""
    assert values.SECRET_KEY.get_secret_value() == ""


def test_every_setting_is_pinned_in_tests() -> None:
    assert set(Settings.model_fields) == set(PINNED_SETTINGS) | {"DATA_DIR", "WEB_DIST_DIR"}


def test_tests_read_the_pinned_settings(tmp_path: Path) -> None:
    values = get_settings()

    assert values.DATABASE_URL == PINNED_SETTINGS["DATABASE_URL"]
    assert values.DATA_DIR == tmp_path
    assert values.MODEL_API_KEY.get_secret_value() == ""


def test_relative_web_dist_dir_is_inside_backend_and_normalised() -> None:
    assert settings(WEB_DIST_DIR="../web/dist").WEB_DIST_DIR == BACKEND_DIR.parent / "web" / "dist"


def test_a_password_with_an_unescaped_at_is_refused_without_showing_it() -> None:
    # Unescaped, the part after "@" would be read as the host, and printed as one.
    with pytest.raises(ValidationError) as error:
        settings(DATABASE_URL="postgresql://app:pa@ss-not-real@db.example.com:5432/app")

    assert "ss-not-real" not in str(error.value)
    assert "%40" in str(error.value)
