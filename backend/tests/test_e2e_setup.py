"""scripts/e2e_setup.py: the browser checks' own database (batch 7)."""

import json
from collections.abc import Iterator

import pytest
from sqlalchemy import select

from countertalk import auth, storage
from countertalk.config import get_settings
from countertalk.db import get_engine, session_scope
from countertalk.models import Org, Transcript, Turn, User, Visit, VisitStatus
from scripts.e2e_setup import main
from tests.conftest import SERVER_URL, drop_database

SCRATCH = "countertalk_unit_e2e"
PASSWORD = "a password for this run only"


def test_a_database_not_named_for_e2e_is_never_dropped(capsys: pytest.CaptureFixture[str]) -> None:
    with session_scope() as session:
        session.add(Org(name="Still Here"))  # in countertalk_test, which conftest points at

    assert main() == 1

    assert "_e2e" in capsys.readouterr().err
    with session_scope(read_only=True) as session:
        assert session.scalars(select(Org.name)).all() == ["Still Here"]


@pytest.fixture
def e2e_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", f"{SERVER_URL}/{SCRATCH}")
    monkeypatch.setenv("E2E_PASSWORD", PASSWORD)
    get_settings.cache_clear()
    yield
    get_engine().dispose()  # the scratch database's engine: settings still point at it
    drop_database(SCRATCH)


def test_it_seeds_a_transcribed_visit_and_a_recording_one(
    e2e_database: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main() == 0

    made = json.loads(capsys.readouterr().out)
    with session_scope(read_only=True) as session:
        transcribed = session.get(Visit, made["transcribed"])
        recording = session.get(Visit, made["recording"])
        assert transcribed is not None and recording is not None
        transcript = session.get(Transcript, transcribed.transcript_id)
        turns = session.scalars(select(Turn).where(Turn.transcript_id == transcribed.transcript_id))
        assert transcript is not None and len(list(turns)) >= 3
    assert (transcribed.status, recording.status) == (
        VisitStatus.TRANSCRIBED,
        VisitStatus.RECORDING,
    )
    assert storage.playback_path(transcribed.id).stat().st_size > 0
    # The checks sign in as the demo rep and K6's finance member (batch 16).
    with session_scope(read_only=True) as session:
        for email in (made["rep"], made["k6"]["email"]):
            stored = session.scalars(select(User.password_hash).where(User.email == email)).one()
            assert auth.password_matches(stored, PASSWORD)


def test_without_a_password_it_stops_before_touching_anything(
    e2e_database: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("E2E_PASSWORD")

    assert main() == 1

    assert capsys.readouterr().err == "no password: set E2E_PASSWORD (the runner makes one)\n"


def test_a_short_password_stops_it_before_anything_is_dropped(
    e2e_database: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("E2E_PASSWORD", "too short")

    assert main() == 1

    assert capsys.readouterr().err == (
        "E2E_PASSWORD: password_too_short: A password needs 12 characters or more\n"
    )
