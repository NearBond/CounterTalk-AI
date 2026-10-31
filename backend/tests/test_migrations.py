from collections.abc import Iterator

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect

from countertalk.config import get_settings
from countertalk.db import get_engine
from countertalk.models import Base
from tests.conftest import SERVER_URL, alembic_config, create_database, drop_database

SCRATCH_DATABASE = "countertalk_test_migrations"
MODEL_TABLES = {table.name for table in Base.metadata.sorted_tables}


@pytest.fixture(autouse=True)
def empty_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Points the app and Alembic at a new, empty database for the length of the test."""
    drop_database(SCRATCH_DATABASE)
    create_database(SCRATCH_DATABASE)
    monkeypatch.setenv("DATABASE_URL", f"{SERVER_URL}/{SCRATCH_DATABASE}")
    get_settings.cache_clear()
    yield
    get_engine().dispose()
    drop_database(SCRATCH_DATABASE)


def table_names() -> set[str]:
    return set(inspect(get_engine()).get_table_names())


def test_empty_database_upgrades_to_every_table() -> None:
    command.upgrade(alembic_config(), "head")

    assert {"orgs", "users", "shops"} <= MODEL_TABLES
    assert table_names() == MODEL_TABLES | {"alembic_version"}


def test_migrations_match_the_models() -> None:
    command.upgrade(alembic_config(), "head")

    with get_engine().connect() as conn:
        differences = compare_metadata(MigrationContext.configure(conn), Base.metadata)

    assert differences == []


def test_downgrade_to_base_then_upgrade_again() -> None:
    config = alembic_config()
    command.upgrade(config, "head")

    command.downgrade(config, "base")
    assert table_names() == {"alembic_version"}

    command.upgrade(config, "head")
    assert table_names() == MODEL_TABLES | {"alembic_version"}
