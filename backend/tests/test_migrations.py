import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text

from countertalk.config import get_settings
from countertalk.db import get_engine
from countertalk.models import Base
from tests.conftest import SERVER_URL, alembic_config, create_database, drop_database

SCRATCH_DATABASE = "countertalk_test_migrations"
MODEL_TABLES = {table.name for table in Base.metadata.sorted_tables}
# Batch 10's: it adds extraction as an llm_calls purpose.
EXTRACTION_REVISION = "4dea182093b4"
# Batch 17's: consents, and when a visit was confirmed.
CONSENT_REVISION = "7c1f4e2a9d03"


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


def test_downgrading_past_extraction_drops_its_calls_and_keeps_the_others() -> None:
    config = alembic_config()
    command.upgrade(config, "head")
    calls = (
        "INSERT INTO llm_calls (id, purpose, provider, model, prompt_hash, latency_ms, ok,"
        " created_at) VALUES (gen_random_uuid(), :purpose, 'fake', 'fake-1', 'h', 1, true, now())"
    )
    with get_engine().begin() as conn:
        for purpose in ("roles", "extraction"):
            conn.execute(text(calls), {"purpose": purpose})

    command.downgrade(config, EXTRACTION_REVISION + "-1")

    with get_engine().connect() as conn:
        assert conn.scalars(text("SELECT purpose FROM llm_calls")).all() == ["roles"]


def test_visits_confirmed_before_batch_17_get_their_confirmation_time() -> None:
    config = alembic_config()
    command.upgrade(config, CONSENT_REVISION + "-1")
    # One confirmed with its Edit log row, one confirmed with none, one still in review.
    rows = """
        INSERT INTO orgs (id, name, created_at) VALUES ('{o}', 'Old Org', now());
        INSERT INTO users (id, org_id, email, name, role, created_at)
            VALUES ('{u}', '{o}', 'old@example.com', 'Old Rep', 'rep', now());
        INSERT INTO shops (id, org_id, name, area, do_not_record, created_at)
            VALUES ('{s}', '{o}', 'Old Shop', 'Old Area', false, now());
        INSERT INTO visits (id, org_id, shop_id, rep_id, kind, client, status,
            stt_seconds_billed, stt_wall_ms, created_at, updated_at)
        VALUES
            ('{a}', '{o}', '{s}', '{u}', 'conversation', 'web', 'confirmed', 0, 0,
             '2026-10-01Z', '2026-10-09Z'),
            ('{b}', '{o}', '{s}', '{u}', 'conversation', 'web', 'confirmed', 0, 0,
             '2026-10-01Z', '2026-10-08Z'),
            ('{c}', '{o}', '{s}', '{u}', 'conversation', 'web', 'review', 0, 0,
             '2026-10-01Z', '2026-10-07Z');
        INSERT INTO edits (id, org_id, visit_id, actor_id, object, object_id, field, before,
            after, created_at)
        VALUES (gen_random_uuid(), '{o}', '{a}', '{u}', 'visit', '{a}', 'status', '"review"',
            '"confirmed"', '2026-10-02T10:00:00Z');
    """
    ids = {key: str(uuid.uuid4()) for key in "ousabc"}
    with get_engine().begin() as conn:
        conn.execute(text(rows.format(**ids)))

    command.upgrade(config, "head")

    with get_engine().connect() as conn:
        found = dict(conn.execute(text("SELECT id::text, confirmed_at FROM visits")).all())
    assert found == {
        ids["a"]: datetime(2026, 10, 2, 10, tzinfo=UTC),  # the Edit log's time
        ids["b"]: datetime(2026, 10, 8, tzinfo=UTC),  # its last update, with no log row
        ids["c"]: None,
    }
