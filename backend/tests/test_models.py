import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy import (
    Column,
    Executable,
    ForeignKey,
    Integer,
    MetaData,
    Table,
    delete,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError, StatementError

from countertalk.db import constraint_name, session_scope
from countertalk.models import (
    IST,
    Base,
    Distributor,
    Evidence,
    Extraction,
    Insight,
    Job,
    JobKind,
    JobState,
    Org,
    Report,
    Role,
    Segment,
    Shop,
    Task,
    Team,
    Transcript,
    Turn,
    User,
    Visit,
    ist_date,
    new_id,
    utc_now,
)
from tests.conftest import give_transcript


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


def a_team(org_id: str) -> str:
    with session_scope() as session:
        team = Team(org_id=org_id, key="finance", name="Finance")
        session.add(team)
        session.flush()
        return team.id


@pytest.mark.parametrize("role", list(Role))
def test_every_role_is_accepted(role: Role) -> None:
    org_id = add_org()
    team_id = a_team(org_id) if role == Role.TEAM_MEMBER else None

    with session_scope() as session:
        email = f"{role}@example.com"
        session.add(User(org_id=org_id, email=email, name="Test", role=role, team_id=team_id))


@pytest.mark.parametrize(
    ("role", "email", "team", "constraint"),
    [
        ("superuser", "boss@example.com", False, "ck_users_role"),
        (Role.REP, "Rep@Example.com", False, "ck_users_email_lowercase"),
        # A team member has a team, and only a team member (batch 16).
        (Role.TEAM_MEMBER, "member@example.com", False, "ck_users_team_member_team"),
        (Role.MANAGER, "manager@example.com", True, "ck_users_team_member_team"),
    ],
)
def test_a_bad_user_is_refused_by_the_database(
    role: str, email: str, team: bool, constraint: str
) -> None:
    org_id = add_org()
    team_id = a_team(org_id) if team else None

    with pytest.raises(IntegrityError) as error, session_scope() as session:
        session.add(User(org_id=org_id, email=email, name="Test", role=role, team_id=team_id))

    assert constraint_name(error.value) == constraint


def test_a_team_member_s_team_is_in_their_own_org() -> None:
    theirs, other = add_org(), add_org("Other Org")
    other_team = a_team(other)

    with pytest.raises(IntegrityError) as error, session_scope() as session:
        session.add(
            User(
                org_id=theirs,
                email="member@example.com",
                name="Test",
                role=Role.TEAM_MEMBER,
                team_id=other_team,
            )
        )

    assert constraint_name(error.value) == "fk_users_team_id_teams"


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

    assert constraint_name(error.value) == constraint


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

    assert constraint_name(error.value) == constraint


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

    assert constraint_name(error.value) == constraint


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

    assert constraint_name(error.value) == constraint


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

    assert constraint_name(error.value) == constraint


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

    assert constraint_name(error.value) == "ck_turns_times"


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

    assert constraint_name(error.value) == "fk_visits_id_transcripts"


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

    assert constraint_name(error.value) == "fk_shops_org_id_distributors"


def test_a_visits_local_date_is_indias() -> None:
    # 20:00 UTC on 1 Nov is 01:30 on 2 Nov in India.
    assert ist_date(datetime(2026, 11, 1, 20, 0, tzinfo=UTC)).isoformat() == "2026-11-02"


def extracted(visit_id: str, transcript_id: str, org_id: str, **insight: object) -> str:
    """An extraction with a report, a task, and an insight with one quote of turn 0: its
    insight's ID."""
    with session_scope() as session:
        extraction = Extraction(
            visit_id=visit_id,
            transcript_id=transcript_id,
            playbook_id="fmcg-kirana-visit",
            playbook_version=1,
            org_pack_id="tiny-demo",
            org_pack_version=1,
            model="fake-1",
            prompt_hash="ab" * 32,
            raw_json={"insights": []},
        )
        session.add(extraction)
        session.flush()
        session.add(Report(visit_id=visit_id, extraction_id=extraction.id, summary_en="S", json={}))
        session.add(
            Task(visit_id=visit_id, seq=0, type="callback", description_en="Call", owner="rep")
        )
        values: dict[str, object] = {
            "org_id": org_id,
            "visit_id": visit_id,
            "seq": 0,
            "extraction_id": extraction.id,
            "kind": "problem",
            "category": "supply",
            "severity": "medium",
            "summary_en": "Late delivery.",
            "sku_mentions": [],
            "competitor_mentions": [],
        }
        row = Insight(**{**values, **insight})
        session.add(row)
        session.flush()
        session.add(
            Evidence(
                insight_id=row.id,
                visit_id=visit_id,
                seq=0,
                transcript_id=transcript_id,
                turn_id=0,
                quote_llm="Namaste ji.",
            )
        )
        return row.id


def test_a_visits_extractions_and_their_rows_go_with_it() -> None:
    visit_id, transcript_id = add_transcript()
    with session_scope(read_only=True) as session:
        org_id = session.get_one(Visit, visit_id).org_id
    extracted(visit_id, transcript_id, org_id)

    with session_scope() as session:
        session.delete(session.get(Visit, visit_id))

    with session_scope(read_only=True) as session:
        for table in (Extraction, Report, Task, Insight, Evidence):
            assert session.scalars(select(table)).all() == []


def test_an_insights_visit_and_suggested_team_are_its_own_orgs() -> None:
    visit_id, transcript_id = add_transcript()
    with session_scope() as session:
        org_id = session.get_one(Visit, visit_id).org_id
        other = Org(name="Other Org")
        session.add(other)
        session.flush()
        theirs = Team(org_id=other.id, key="money", name="Money desk")
        session.add(theirs)
        session.flush()
        other_org, their_team = other.id, theirs.id

    with pytest.raises(IntegrityError) as wrong_org:
        extracted(visit_id, transcript_id, other_org)
    with pytest.raises(IntegrityError) as wrong_team:
        extracted(visit_id, transcript_id, org_id, ai_team_id=their_team)

    assert constraint_name(wrong_org.value) == "fk_insights_org_id_visits"
    assert constraint_name(wrong_team.value) == "fk_insights_ai_team_id_teams"


@pytest.mark.parametrize(
    ("change", "constraint"),
    [
        ({"turn_id": 99}, "fk_evidence_transcript_id_turns"),
        ({"match": "maybe"}, "ck_evidence_match"),
        ({"char_start": 5, "char_end": 4}, "ck_evidence_chars"),
        ({"char_start": -1, "char_end": 4}, "ck_evidence_chars"),
        ({"start_ms": -1}, "ck_evidence_start_ms"),
    ],
    ids=["no-such-turn", "unknown-match", "chars-backwards", "chars-negative", "time-negative"],
)
def test_evidence_refuses_a_turn_its_transcript_hasnt_got_and_an_unknown_match(
    change: dict[str, object], constraint: str
) -> None:
    visit_id, transcript_id = add_transcript()
    with session_scope(read_only=True) as session:
        org_id = session.get_one(Visit, visit_id).org_id
    insight_id = extracted(visit_id, transcript_id, org_id)

    with pytest.raises(IntegrityError) as error, session_scope() as session:
        values = {
            "insight_id": insight_id,
            "visit_id": visit_id,
            "seq": 1,
            "transcript_id": transcript_id,
        }
        session.add(Evidence(**{**values, "turn_id": None, "quote_llm": "x", **change}))

    assert constraint_name(error.value) == constraint


def test_a_naive_time_has_no_local_date() -> None:
    with pytest.raises(ValueError):
        ist_date(datetime(2026, 11, 1, 20, 0))  # noqa: DTZ001 - a naive datetime is under test


@pytest.mark.parametrize(
    ("change", "constraint"),
    [
        ({"visible": True}, "ck_insights_visible_only_verified"),
        ({"added_by_rep": True}, "ck_insights_extracted_or_added"),
    ],
    ids=["visible-unverified", "added-yet-extracted"],
)
def test_an_insight_is_shown_only_verified_and_is_extracted_or_added(
    change: dict[str, object], constraint: str
) -> None:
    visit_id, transcript_id = add_transcript()
    with session_scope(read_only=True) as session:
        org_id = session.get_one(Visit, visit_id).org_id

    with pytest.raises(IntegrityError) as error:
        extracted(visit_id, transcript_id, org_id, **change)

    assert constraint_name(error.value) == constraint


def test_rows_never_point_at_another_visits_extraction_or_transcript() -> None:
    mine, my_transcript = add_transcript()
    with session_scope(read_only=True) as session:
        v = session.get_one(Visit, mine)
        org_id, parts = v.org_id, {"org_id": v.org_id, "rep_id": v.rep_id, "shop_id": v.shop_id}
    theirs, their_transcript = add_transcript(
        Visit(**parts, kind="conversation", client="web", status="recording")
    )
    extracted(theirs, their_transcript, org_id)
    my_insight = extracted(mine, my_transcript, org_id)
    with session_scope(read_only=True) as session:
        their_extraction = session.scalars(
            select(Extraction.id).where(Extraction.visit_id == theirs)
        ).one()

    def refused(row: object) -> str | None:
        with pytest.raises(IntegrityError) as error, session_scope() as session:
            session.add(row)
        return constraint_name(error.value)

    report = Report(visit_id=mine, extraction_id=their_extraction, summary_en="S", json={})
    quote = Evidence(
        insight_id=my_insight,
        visit_id=mine,
        seq=1,
        transcript_id=their_transcript,
        quote_llm="x",
    )
    with session_scope() as session:
        session.execute(delete(Report).where(Report.visit_id == mine))
    assert refused(report) == "fk_reports_visit_id_extractions"
    assert refused(quote) == "fk_evidence_visit_id_transcripts"


def foreign_keys_across_orgs(metadata: MetaData) -> list[str]:
    """R9: a foreign key from a table with org_id to another table with org_id must include
    org_id, so the database refuses a row that points into another org. Added in batch 14, when
    R9 came up twice in one part."""
    found = []
    for table in metadata.sorted_tables:
        if "org_id" not in table.c:
            continue
        for key in table.foreign_key_constraints:
            target = key.referred_table
            if target.name != "orgs" and "org_id" in target.c and "org_id" not in key.column_keys:
                found.append(f"{table.name} ({', '.join(key.column_keys)}) -> {target.name}")
    return found


def test_every_org_scoped_row_points_only_at_its_own_org_s_rows() -> None:
    assert foreign_keys_across_orgs(Base.metadata) == []


def test_the_check_finds_a_key_without_its_org() -> None:
    metadata = MetaData()
    Table("orgs", metadata, Column("id", Integer, primary_key=True))
    Table("teams", metadata, Column("id", Integer, primary_key=True), Column("org_id", Integer))
    Table(
        "notes",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("org_id", Integer, ForeignKey("orgs.id")),
        Column("team_id", Integer, ForeignKey("teams.id")),
    )

    assert foreign_keys_across_orgs(metadata) == ["notes (team_id) -> teams"]
