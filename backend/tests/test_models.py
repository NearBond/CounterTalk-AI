import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import Executable, delete, select, text, update
from sqlalchemy.exc import IntegrityError, StatementError

from countertalk.db import session_scope
from countertalk.models import (
    Distributor,
    Job,
    JobKind,
    JobState,
    Org,
    Role,
    Segment,
    Shop,
    Transcript,
    Turn,
    User,
    Visit,
    new_id,
    utc_now,
)
from tests.conftest import give_transcript

IST = timezone(timedelta(hours=5, minutes=30))


def constraint_violated(error: IntegrityError) -> str | None:
    diag = getattr(error.orig, "diag", None)
    return getattr(diag, "constraint_name", None)


def add_org(name: str = "Test Org") -> str:
    with session_scope() as session:
        org = Org(name=name)
        session.add(org)
    return org.id


def add_org_at(created_at: datetime) -> str:
    with session_scope() as session:
        org = Org(name="Timed Org", created_at=created_at)
        session.add(org)
    return org.id


def test_new_id_is_a_uuid4_string() -> None:
    value = new_id()

    assert isinstance(value, str)
    assert uuid.UUID(value).version == 4


def test_utc_now_is_timezone_aware_utc() -> None:
    assert utc_now().tzinfo is UTC


def test_naive_datetime_is_rejected() -> None:
    naive = datetime(2026, 11, 1, 9, 30)  # noqa: DTZ001 - a naive datetime is what's under test

    with pytest.raises(StatementError, match="naive"), session_scope() as session:
        session.add(Org(name="Naive Org", created_at=naive))
        session.flush()


def test_created_at_reads_back_as_utc_whatever_the_connection_time_zone() -> None:
    written = datetime(2026, 11, 1, 15, 0, tzinfo=IST)
    org_id = add_org_at(written)

    with session_scope(read_only=True) as session:
        # LOCAL: ends with the transaction, so the pooled connection doesn't keep it.
        session.execute(text("SET LOCAL TIME ZONE 'Asia/Kolkata'"))
        created_at = session.scalar(select(Org.created_at).where(Org.id == org_id))
    with session_scope(read_only=True) as session:
        time_zone_after = session.scalar(text("SHOW TIME ZONE"))

    assert time_zone_after != "Asia/Kolkata"
    assert created_at is not None
    assert created_at.tzinfo is UTC
    assert created_at == written


@pytest.mark.parametrize("role", list(Role))
def test_every_role_is_accepted(role: Role) -> None:
    org_id = add_org()

    with session_scope() as session:
        session.add(User(org_id=org_id, email=f"{role}@example.com", name="Test", role=role))


@pytest.mark.parametrize(
    ("role", "email", "constraint"),
    [
        ("superuser", "boss@example.com", "ck_users_role"),
        (Role.REP, "Rep@Example.com", "ck_users_email_lowercase"),
    ],
)
def test_a_bad_user_is_refused_by_the_database(role: str, email: str, constraint: str) -> None:
    org_id = add_org()

    with pytest.raises(IntegrityError) as error, session_scope() as session:
        session.add(User(org_id=org_id, email=email, name="Test", role=role))

    assert constraint_violated(error.value) == constraint


@pytest.mark.parametrize(
    ("make_duplicate", "constraint"),
    [
        (lambda org_id: Org(name="Test Org"), "uq_orgs_name"),
        (
            lambda org_id: User(org_id=org_id, email="dup@example.com", name="B", role=Role.REP),
            "uq_users_email",
        ),
        (
            lambda org_id: Shop(org_id=org_id, name="Dup Shop", area="Dup Area"),
            "uq_shops_org_id_name_area",
        ),
    ],
)
def test_natural_keys_are_unique(make_duplicate: Callable[[str], object], constraint: str) -> None:
    org_id = add_org("Test Org")
    with session_scope() as session:
        session.add(User(org_id=org_id, email="dup@example.com", name="A", role=Role.REP))
        session.add(Shop(org_id=org_id, name="Dup Shop", area="Dup Area"))

    with pytest.raises(IntegrityError) as error, session_scope() as session:
        session.add(make_duplicate(org_id))

    assert constraint_violated(error.value) == constraint


def add_visit_parts() -> tuple[str, str, str]:
    with session_scope() as session:
        org = Org(name="Visit Model Org")
        session.add(org)
        session.flush()
        rep = User(org_id=org.id, email="model-rep@example.com", name="Rep", role=Role.REP)
        shop = Shop(org_id=org.id, name="Model Shop", area="Model Area")
        session.add_all([rep, shop])
        session.flush()
        return org.id, rep.id, shop.id


def visit_row(**overrides: object) -> Visit:
    org_id, rep_id, shop_id = add_visit_parts()
    values: dict[str, object] = {
        "org_id": org_id,
        "rep_id": rep_id,
        "shop_id": shop_id,
        "kind": "conversation",
        "client": "web",
        "status": "recording",
    }
    return Visit(**{**values, **overrides})


@pytest.mark.parametrize(
    ("field", "value", "constraint"),
    [
        ("status", "archived", "ck_visits_status"),
        ("kind", "meeting", "ck_visits_kind"),
        ("client", "desktop", "ck_visits_client"),
    ],
)
def test_an_unknown_visit_value_is_refused_by_the_database(
    field: str, value: str, constraint: str
) -> None:
    with pytest.raises(IntegrityError) as error, session_scope() as session:
        session.add(visit_row(**{field: value}))

    assert constraint_violated(error.value) == constraint


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"seq": -1}, "ck_segments_seq_not_negative"),
        ({"sha256": "ABC"}, "ck_segments_sha256_hex"),
        ({"bytes": 0}, "ck_segments_bytes_positive"),
    ],
)
def test_a_bad_segment_is_refused_by_the_database(overrides: dict, constraint: str) -> None:
    visit = visit_row()
    with session_scope() as session:
        session.add(visit)
    values = {"visit_id": visit.id, "seq": 0, "path": "x", "bytes": 1, "sha256": "a" * 64}

    with pytest.raises(IntegrityError) as error, session_scope() as session:
        session.add(Segment(**{**values, **overrides}))

    assert constraint_violated(error.value) == constraint


def test_a_visits_segments_go_with_it() -> None:
    visit = visit_row()
    with session_scope() as session:
        session.add(visit)
        session.flush()
        session.add(Segment(visit_id=visit.id, seq=0, path="x", bytes=1, sha256="a" * 64))

    with session_scope() as session:
        session.delete(session.get(Visit, visit.id))

    with session_scope(read_only=True) as session:
        assert session.scalars(select(Segment)).all() == []


@pytest.mark.parametrize(
    ("field", "constraint"),
    [("shop_id", "fk_visits_org_id_shops"), ("rep_id", "fk_visits_org_id_users")],
)
def test_a_visit_with_another_orgs_shop_or_rep_is_refused(field: str, constraint: str) -> None:
    visit = visit_row()
    with session_scope() as session:
        other = Org(name="Another Org")
        session.add(other)
        session.flush()
        other_rep = User(org_id=other.id, email="other-rep@example.com", name="Rep", role=Role.REP)
        other_shop = Shop(org_id=other.id, name="Other Shop", area="Other Area")
        session.add_all([other_rep, other_shop])
        session.flush()
        theirs = {"shop_id": other_shop.id, "rep_id": other_rep.id}[field]

    with pytest.raises(IntegrityError) as error, session_scope() as session:
        setattr(visit, field, theirs)
        session.add(visit)

    assert constraint_violated(error.value) == constraint


# --- jobs (batch 4) -------------------------------------------------------------------------


def job_row(**overrides: object) -> Job:
    values: dict[str, object] = {"kind": JobKind.PING, "state": JobState.QUEUED, "max_attempts": 5}
    return Job(**{**values, **overrides})


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"state": "paused"}, "ck_jobs_state"),
        ({"kind": "dance"}, "ck_jobs_kind"),
        ({"attempts": -1}, "ck_jobs_attempts"),
        ({"max_attempts": 0}, "ck_jobs_attempts"),
        # Running means claimed: it says who has it and since when, and nothing else does.
        ({"state": JobState.RUNNING}, "ck_jobs_claimed_when_running"),
        ({"locked_by": "someone", "locked_at": utc_now()}, "ck_jobs_claimed_when_running"),
    ],
)
def test_a_bad_job_is_refused_by_the_database(overrides: dict, constraint: str) -> None:
    with pytest.raises(IntegrityError) as error, session_scope() as session:
        session.add(job_row(**overrides))

    assert constraint_violated(error.value) == constraint


def test_a_visits_jobs_go_with_it() -> None:
    visit = visit_row()
    with session_scope() as session:
        session.add(visit)
        session.flush()
        session.add(job_row(kind=JobKind.TRANSCRIBE, visit_id=visit.id))

    with session_scope() as session:
        session.delete(session.get(Visit, visit.id))

    with session_scope(read_only=True) as session:
        assert session.scalars(select(Job)).all() == []


# --- transcripts (batch 6) --------------------------------------------------------------------


def add_transcript(visit: Visit | None = None) -> tuple[str, str]:
    """A visit with a transcript of one turn: (visit_id, transcript_id)."""
    visit = visit or visit_row()
    with session_scope() as session:
        session.add(visit)
    return visit.id, give_transcript(visit.id, [("c0:A", 0, "Namaste ji.")])


def turn_row(**overrides: object) -> Turn:
    values: dict[str, object] = {
        "turn_id": 0,
        "speaker_label": "c0:A",
        "start_ms": 0,
        "end_ms": 1_000,
        "text": "Namaste ji.",
    }
    return Turn(**{**values, **overrides})


@pytest.mark.parametrize(
    "change",
    [update(Turn).values(text="Edited."), delete(Turn)],
    ids=["update", "delete"],
)
def test_a_turn_never_changes(change: Executable) -> None:
    add_transcript()

    with pytest.raises(IntegrityError, match="turns rows never change"), session_scope() as session:
        session.execute(change)


def test_a_visits_transcripts_and_turns_go_with_it() -> None:
    visit_id, _ = add_transcript()

    with session_scope() as session:
        session.delete(session.get(Visit, visit_id))

    with session_scope(read_only=True) as session:
        assert session.scalars(select(Transcript)).all() == []
        assert session.scalars(select(Turn)).all() == []


@pytest.mark.parametrize(
    "overrides", [{"start_ms": -1}, {"start_ms": 500, "end_ms": 499}], ids=["negative", "backwards"]
)
def test_a_turn_with_bad_times_is_refused_by_the_database(overrides: dict) -> None:
    _, transcript_id = add_transcript()

    with pytest.raises(IntegrityError) as error, session_scope() as session:
        session.add(turn_row(transcript_id=transcript_id, turn_id=1, **overrides))

    assert constraint_violated(error.value) == "ck_turns_times"


def test_a_visit_pointing_at_another_visits_transcript_is_refused() -> None:
    _, theirs = add_transcript()
    with session_scope() as session:
        org_id = session.scalars(select(Org.id)).one()
        rep_id = session.scalars(select(User.id)).one()
        shop_id = session.scalars(select(Shop.id)).one()
        mine = Visit(
            org_id=org_id, rep_id=rep_id, shop_id=shop_id, kind="conversation", client="web"
        )
        session.add(mine)

    with pytest.raises(IntegrityError) as error, session_scope() as session:
        session.get(Visit, mine.id).transcript_id = theirs  # type: ignore[union-attr]

    assert constraint_violated(error.value) == "fk_visits_id_transcripts"


def test_a_shop_with_another_orgs_distributor_is_refused() -> None:
    org_id, _, shop_id = add_visit_parts()
    with session_scope() as session:
        other = Org(name="Distributor's Org")
        session.add(other)
        session.flush()
        theirs = Distributor(org_id=other.id, key="d1", name="Elsewhere (example)", area="Far")
        session.add(theirs)
        session.flush()
        theirs_id = theirs.id

    with pytest.raises(IntegrityError) as error, session_scope() as session:
        shop = session.get(Shop, shop_id)
        assert shop is not None
        shop.distributor_id = theirs_id

    assert constraint_violated(error.value) == "fk_shops_org_id_distributors"
