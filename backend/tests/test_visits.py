"""visits.py: status changes, locking, creating visits, segments, complete and listing."""

import base64
import hashlib
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from countertalk import consent, shops, storage, visits
from countertalk.db import session_scope
from countertalk.errors import Conflict, Invalid, NotFound, TooLarge
from countertalk.limits import ABANDONED_UPLOAD_MS, LOCK_TIMEOUT_MS, MAX_SEGMENTS_PER_VISIT
from countertalk.models import (
    Job,
    JobKind,
    JobState,
    Org,
    Role,
    Segment,
    Shop,
    User,
    Visit,
    VisitClient,
    VisitKind,
    VisitStatus,
    new_id,
    utc_now,
)

# Long enough for a thread to reach a lock, well under the lock timeout.
BLOCKED_S = 0.5
RACE_TIMEOUT_S = 2 * LOCK_TIMEOUT_MS / 1000


@dataclass(frozen=True)
class Org_:
    org_id: str
    rep_id: str
    shop_id: str


def make_org(name: str = "Visit Test Org") -> Org_:
    with session_scope() as session:
        org = Org(name=name)
        session.add(org)
        session.flush()
        rep = User(org_id=org.id, email=f"rep-{org.id[:8]}@example.com", name="Rep", role=Role.REP)
        shop = Shop(org_id=org.id, name="Test Shop", area="Test Area")
        session.add_all([rep, shop])
        session.flush()
        # Batch 17: it agreed to recording on paper, so a conversation can start.
        consent.mark_unanswered(session, org.id, rep.id, "hi")
        return Org_(org.id, rep.id, shop.id)


@pytest.fixture
def org() -> Org_:
    return make_org()


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def new_visit(org: Org_, kind: str = VisitKind.CONVERSATION, client: str = VisitClient.WEB) -> str:
    with session_scope() as session:
        return visits.create_visit(session, org.org_id, org.rep_id, org.shop_id, kind, client).id


def upload(org: Org_, visit_id: str, seq: int, body: bytes = b"segment bytes") -> bool:
    with visits.stage_segment(seq, body, sha(body)) as staged, session_scope() as session:
        return visits.store_staged_segment(session, org.org_id, visit_id, staged).created


def complete(org: Org_, visit_id: str, last_seq: int, ext: str = "webm") -> Visit:
    with session_scope() as session:
        return visits.complete_visit(session, org.org_id, visit_id, last_seq, ext)


def move_to(org: Org_, visit_id: str, *steps: str, reason: str | None = None) -> None:
    with session_scope() as session:
        visit = visits.lock_visit(session, org.org_id, visit_id)
        for step in steps:
            visits.set_status(visit, step, reason=reason)


def load(visit_id: str) -> Visit:
    with session_scope(read_only=True) as session:
        visit = session.get(Visit, visit_id)
        assert visit is not None
        return visit


def staged_files(data_dir: Path) -> list[Path]:
    staging = data_dir / "tmp"
    return sorted(staging.iterdir()) if staging.exists() else []


# --- set_status -------------------------------------------------------------------------------

FORWARD = [
    (VisitStatus.RECORDING, VisitStatus.UPLOADED),
    (VisitStatus.UPLOADED, VisitStatus.TRANSCRIBING),
    (VisitStatus.TRANSCRIBING, VisitStatus.TRANSCRIBED),
    (VisitStatus.TRANSCRIBED, VisitStatus.EXTRACTING),
    (VisitStatus.EXTRACTING, VisitStatus.REVIEW),
    (VisitStatus.REVIEW, VisitStatus.CONFIRMED),
]


@pytest.mark.parametrize(("start", "end"), FORWARD)
def test_each_forward_step_is_allowed(start: str, end: str) -> None:
    visit = Visit(status=start, client=VisitClient.WEB)

    visits.set_status(visit, end)

    assert visit.status == end


@pytest.mark.parametrize(
    "start", [s for s in VisitStatus if s not in (VisitStatus.FAILED, VisitStatus.CONFIRMED)]
)
def test_any_status_can_fail_with_a_reason(start: str) -> None:
    visit = Visit(status=start, client=VisitClient.WEB)

    visits.set_status(visit, VisitStatus.FAILED, reason="The audio can't be read.")

    assert (visit.status, visit.failure_reason) == (VisitStatus.FAILED, "The audio can't be read.")


def test_a_confirmed_visit_never_fails() -> None:
    # Confirmed is final: its confirmed_at is when its audio's retention began (batch 17).
    visit = Visit(status=VisitStatus.CONFIRMED, client=VisitClient.WEB)

    with pytest.raises(Conflict) as error:
        visits.set_status(visit, VisitStatus.FAILED, reason="The audio can't be read.")

    assert (error.value.code, str(error.value)) == (
        "invalid_status_change",
        "invalid_status_change: A confirmed visit can't change",
    )


def test_failing_needs_a_reason() -> None:
    with pytest.raises(ValueError):
        visits.set_status(Visit(status=VisitStatus.RECORDING), VisitStatus.FAILED)


def visit_in(status: str, **values: object) -> Visit:
    return Visit(
        **{"status": status, "client": VisitClient.WEB, "completed_at": utc_now(), **values}
    )


@pytest.mark.parametrize(
    ("visit", "end"),
    [
        (visit_in(VisitStatus.RECORDING), VisitStatus.REVIEW),
        (visit_in(VisitStatus.UPLOADED), VisitStatus.RECORDING),
        (visit_in(VisitStatus.CONFIRMED), VisitStatus.REVIEW),
        (visit_in(VisitStatus.REVIEW), VisitStatus.RECORDING),
        (visit_in(VisitStatus.RECORDING), VisitStatus.RECORDING),
        (visit_in(VisitStatus.CONFIRMED), VisitStatus.UPLOADED),
        # Only an abandoned web or upload visit comes back.
        (
            visit_in(VisitStatus.FAILED, client=VisitClient.PHONE, abandoned_at=utc_now()),
            VisitStatus.RECORDING,
        ),
        (visit_in(VisitStatus.FAILED, abandoned_at=None), VisitStatus.RECORDING),
        # Reprocessing from transcription needs a completed upload.
        (visit_in(VisitStatus.FAILED, completed_at=None), VisitStatus.UPLOADED),
    ],
)
def test_a_step_not_allowed_is_refused(visit: Visit, end: str) -> None:
    start = visit.status

    with pytest.raises(Conflict) as error:
        visits.set_status(visit, end)

    assert error.value.code == "invalid_status_change"
    assert visit.status == start


@pytest.mark.parametrize("client", [VisitClient.WEB, VisitClient.UPLOAD])
def test_an_abandoned_web_or_upload_visit_can_come_back(client: str) -> None:
    visit = Visit(status=VisitStatus.FAILED, client=client, abandoned_at=utc_now())
    visit.failure_reason = "Nothing was uploaded for 12 hours."

    visits.set_status(visit, VisitStatus.RECORDING)

    assert (visit.status, visit.abandoned_at, visit.failure_reason) == (
        VisitStatus.RECORDING,
        None,
        None,
    )


@pytest.mark.parametrize("start", [VisitStatus.REVIEW, VisitStatus.FAILED])
@pytest.mark.parametrize("end", [VisitStatus.UPLOADED, VisitStatus.TRANSCRIBED])
def test_reprocess_steps_back_before_confirm(start: str, end: str) -> None:
    visit = Visit(status=start, client=VisitClient.WEB, completed_at=utc_now())
    visit.failure_reason = "earlier failure" if start == VisitStatus.FAILED else None

    visits.set_status(visit, end)

    assert (visit.status, visit.failure_reason) == (end, None)


# --- lock_visit and get_visit -----------------------------------------------------------------


def test_another_orgs_visit_is_not_found(org: Org_) -> None:
    other = make_org("Other Org")
    visit_id = new_visit(other)

    with pytest.raises(NotFound), session_scope() as session:
        visits.lock_visit(session, org.org_id, visit_id)
    with pytest.raises(NotFound), session_scope(read_only=True) as session:
        visits.get_visit(session, org.org_id, visit_id)


@pytest.mark.parametrize("visit_id", ["not-a-uuid", "", new_id()])
def test_a_malformed_or_missing_visit_id_is_not_found(org: Org_, visit_id: str) -> None:
    with pytest.raises(NotFound), session_scope(read_only=True) as session:
        visits.get_visit(session, org.org_id, visit_id)


# --- create_visit -----------------------------------------------------------------------------


def test_a_new_visit_is_recording(org: Org_) -> None:
    visit = load(new_visit(org))

    assert (visit.status, visit.kind, visit.client) == ("recording", "conversation", "web")
    assert (visit.org_id, visit.rep_id, visit.shop_id) == (org.org_id, org.rep_id, org.shop_id)
    assert visit.completed_at is None


def test_a_shop_in_another_org_is_not_found(org: Org_) -> None:
    other = make_org("Other Org")

    with pytest.raises(NotFound), session_scope() as session:
        visits.create_visit(session, org.org_id, org.rep_id, other.shop_id, "conversation", "web")


def test_a_do_not_record_shop_refuses_a_conversation_but_takes_notes(org: Org_) -> None:
    with session_scope() as session:
        shop = session.get(Shop, org.shop_id)
        assert shop is not None
        shop.do_not_record = True

    with pytest.raises(Conflict) as error:
        new_visit(org, kind=VisitKind.CONVERSATION)

    assert error.value.code == "shop_do_not_record"
    assert load(new_visit(org, kind=VisitKind.NOTES)).kind == "notes"


@pytest.mark.parametrize(("kind", "client"), [("meeting", "web"), ("notes", "desktop")])
def test_an_unknown_kind_or_client_is_refused(org: Org_, kind: str, client: str) -> None:
    with pytest.raises(Invalid):
        new_visit(org, kind=kind, client=client)


def test_a_conversation_waits_for_a_do_not_record_being_set_at_the_same_time(org: Org_) -> None:
    outcome: list[str] = []

    def create() -> None:
        try:
            new_visit(org)
            outcome.append("created")
        except Conflict as error:
            outcome.append(error.code)

    thread = threading.Thread(target=create)
    with session_scope() as holder:
        shop = holder.scalars(select(Shop).where(Shop.id == org.shop_id).with_for_update()).one()
        shop.do_not_record = True
        holder.flush()
        thread.start()
        thread.join(timeout=BLOCKED_S)
        assert thread.is_alive(), "create_visit didn't wait for the shop's change"
    thread.join(timeout=RACE_TIMEOUT_S)

    assert outcome == ["shop_do_not_record"]


# --- segments ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("limit", "body", "checksum_of", "error", "code"),
    [
        (4, b"12345", b"12345", TooLarge, "segment_too_large"),
        (100, b"", b"", Invalid, "empty_segment"),
        (100, b"the bytes", b"other bytes", Invalid, "segment_checksum_mismatch"),
    ],
)
def test_staging_refuses_a_bad_segment_before_anything_is_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: int,
    body: bytes,
    checksum_of: bytes,
    error: type[Exception],
    code: str,
) -> None:
    monkeypatch.setattr(visits, "MAX_SEGMENT_BYTES", limit)

    with pytest.raises(error) as raised:
        visits.stage_segment(0, body, sha(checksum_of))

    assert raised.value.code == code  # type: ignore[attr-defined]
    assert staged_files(tmp_path) == []


def test_a_stored_segment_is_its_file_and_its_row(org: Org_, tmp_path: Path) -> None:
    visit_id = new_visit(org)

    assert upload(org, visit_id, 0, b"first segment") is True

    final = storage.segment_path(visit_id, 0)
    assert final.read_bytes() == b"first segment"
    with session_scope(read_only=True) as session:
        row = session.get(Segment, (visit_id, 0))
    assert row is not None
    assert (row.path, row.bytes, row.sha256) == (
        f"visits/{visit_id}/segments/000000.bin",
        len(b"first segment"),
        sha(b"first segment"),
    )
    assert staged_files(tmp_path) == []


def test_the_same_segment_twice_changes_nothing(org: Org_, tmp_path: Path) -> None:
    visit_id = new_visit(org)
    upload(org, visit_id, 0, b"same bytes")
    received_at = segment_rows(visit_id)[0].received_at

    assert upload(org, visit_id, 0, b"same bytes") is False

    assert [row.received_at for row in segment_rows(visit_id)] == [received_at]
    assert staged_files(tmp_path) == []


@pytest.mark.parametrize(
    ("setup", "seq", "error", "code"),
    [
        ("stored_already", 0, Conflict, "segment_conflict"),
        ("failed", 0, Conflict, "visit_not_recording"),
        ("another_org", 0, NotFound, "visit_not_found"),
        ("no_visit", -1, Invalid, "invalid_seq"),
        ("no_visit", MAX_SEGMENTS_PER_VISIT, Invalid, "invalid_seq"),
    ],
)
def test_a_segment_that_cant_be_stored_is_refused_and_leaves_nothing(
    org: Org_, tmp_path: Path, setup: str, seq: int, error: type[Exception], code: str
) -> None:
    owner = make_org("Other Org") if setup == "another_org" else org
    visit_id = new_id() if setup == "no_visit" else new_visit(owner)
    if setup == "stored_already":
        upload(org, visit_id, 0, b"original")
    if setup == "failed":
        move_to(org, visit_id, VisitStatus.FAILED, reason="Stopped.")

    with pytest.raises(error) as raised:
        upload(org, visit_id, seq, b"something else")

    assert raised.value.code == code  # type: ignore[attr-defined]
    assert staged_files(tmp_path) == []
    if setup == "stored_already":
        assert storage.segment_path(visit_id, 0).read_bytes() == b"original"


def test_a_segment_brings_an_abandoned_web_visit_back(org: Org_) -> None:
    visit_id = new_visit(org)
    abandon(org, visit_id)

    assert upload(org, visit_id, 0) is True

    visit = load(visit_id)
    assert (visit.status, visit.abandoned_at, visit.failure_reason) == ("recording", None, None)


def test_a_segment_upload_waits_for_a_change_to_the_same_visit(org: Org_) -> None:
    visit_id = new_visit(org)
    outcome: list[str] = []

    def upload_now() -> None:
        try:
            upload(org, visit_id, 0)
            outcome.append("stored")
        except Conflict as error:
            outcome.append(error.code)

    thread = threading.Thread(target=upload_now)
    with session_scope() as holder:
        locked = visits.lock_visit(holder, org.org_id, visit_id)
        thread.start()
        thread.join(timeout=BLOCKED_S)
        assert thread.is_alive(), "the upload didn't wait for the visit's lock"
        visits.set_status(locked, VisitStatus.FAILED, reason="Stopped.")
    thread.join(timeout=RACE_TIMEOUT_S)

    assert outcome == ["visit_not_recording"]
    assert segment_rows(visit_id) == []


def test_another_visit_doesnt_wait(org: Org_) -> None:
    locked_id, free_id = new_visit(org), new_visit(org)
    done = threading.Event()

    def upload_other() -> None:
        upload(org, free_id, 0)
        done.set()

    with session_scope() as holder:
        visits.lock_visit(holder, org.org_id, locked_id)
        thread = threading.Thread(target=upload_other)
        thread.start()
        assert done.wait(timeout=RACE_TIMEOUT_S)
    thread.join()


# --- complete_visit ---------------------------------------------------------------------------


def transcribe_jobs(visit_id: str) -> list[str]:
    with session_scope(read_only=True) as session:
        query = select(Job.state).where(Job.visit_id == visit_id, Job.kind == JobKind.TRANSCRIBE)
        return list(session.scalars(query))


def test_complete_with_every_segment_marks_the_visit_uploaded_and_queues_it(org: Org_) -> None:
    visit_id = new_visit(org)
    for seq in range(3):
        upload(org, visit_id, seq, f"part {seq}".encode())

    visit = complete(org, visit_id, last_seq=2, ext="webm")

    assert (visit.status, visit.source_ext) == ("uploaded", "webm")
    assert visit.completed_at is not None
    assert transcribe_jobs(visit_id) == [JobState.QUEUED]


@pytest.mark.parametrize(
    ("uploaded", "last_seq", "code", "details"),
    [
        ([0, 2], 3, "segments_missing", {"missing_seqs": [1, 3]}),
        ([], 0, "segments_missing", {"missing_seqs": [0]}),
        ([0, 1, 2], 1, "unexpected_segments", {"unexpected_seqs": [2]}),
    ],
)
def test_complete_needs_exactly_segments_0_to_last_seq(
    org: Org_, uploaded: list[int], last_seq: int, code: str, details: dict
) -> None:
    visit_id = new_visit(org)
    for seq in uploaded:
        upload(org, visit_id, seq, f"part {seq}".encode())

    with pytest.raises(Conflict) as error:
        complete(org, visit_id, last_seq=last_seq)

    assert (error.value.code, error.value.details) == (code, details)
    assert load(visit_id).status == "recording"


def test_complete_twice_is_safe(org: Org_) -> None:
    visit_id = new_visit(org)
    upload(org, visit_id, 0)
    first = complete(org, visit_id, last_seq=0)

    again = complete(org, visit_id, last_seq=0)

    assert (again.status, again.completed_at) == ("uploaded", first.completed_at)
    assert transcribe_jobs(visit_id) == [JobState.QUEUED]


def test_complete_again_with_other_values_is_a_conflict(org: Org_) -> None:
    visit_id = new_visit(org)
    upload(org, visit_id, 0)
    complete(org, visit_id, last_seq=0, ext="webm")

    with pytest.raises(Conflict) as error:
        complete(org, visit_id, last_seq=0, ext="m4a")

    assert error.value.code == "visit_not_recording"


def test_complete_brings_an_abandoned_web_visit_back(org: Org_) -> None:
    visit_id = new_visit(org)
    upload(org, visit_id, 0)
    abandon(org, visit_id)

    assert complete(org, visit_id, last_seq=0).status == "uploaded"


@pytest.mark.parametrize(
    ("last_seq", "ext", "code"),
    [
        (0, "exe", "unsupported_extension"),
        (-1, "webm", "invalid_seq"),
        (MAX_SEGMENTS_PER_VISIT, "webm", "invalid_seq"),
    ],
)
def test_complete_checks_its_values_before_any_query(
    org: Org_, last_seq: int, ext: str, code: str
) -> None:
    # Another org's visit would be 404: the values are refused first, before any query.
    visit_id = new_visit(make_org("Other Org"))

    with pytest.raises(Invalid) as error:
        complete(org, visit_id, last_seq=last_seq, ext=ext)

    assert error.value.code == code


# --- listing ----------------------------------------------------------------------------------


def list_all(org: Org_, cursor: str | None = None) -> tuple[list[str], str | None]:
    with session_scope(read_only=True) as session:
        page, next_cursor = visits.list_visits(session, org.org_id, cursor)
        return [visit.id for visit in page], next_cursor


def test_visits_are_listed_newest_first_and_same_tick_ones_by_seq(org: Org_) -> None:
    ids = [new_visit(org) for _ in range(3)]
    same_moment = utc_now()
    with session_scope() as session:
        for visit_id in ids:
            visit = session.get(Visit, visit_id)
            assert visit is not None
            visit.created_at = same_moment
    older = new_visit(org)
    with session_scope() as session:
        visit = session.get(Visit, older)
        assert visit is not None
        visit.created_at = same_moment - timedelta(seconds=1)

    listed, next_cursor = list_all(org)

    assert listed == [*reversed(ids), older]
    assert next_cursor is None


def test_pages_follow_on_with_nothing_missed_or_repeated(
    org: Org_, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(visits, "VISITS_PAGE_SIZE", 2)
    ids = [new_visit(org) for _ in range(5)]
    same_moment = utc_now()
    with session_scope() as session:
        for visit_id in ids[1:4]:
            visit = session.get(Visit, visit_id)
            assert visit is not None
            visit.created_at = same_moment

    seen: list[str] = []
    cursor = None
    for _ in range(5):
        page, cursor = list_all(org, cursor)
        seen += page
        if cursor is None:
            break

    assert sorted(seen) == sorted(ids)
    assert len(seen) == len(set(seen))


def test_another_orgs_visits_arent_listed(org: Org_) -> None:
    mine = new_visit(org)
    new_visit(make_org("Other Org"))

    assert list_all(org)[0] == [mine]


def encoded(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "cursor",
    [
        "garbage!",
        "",
        encoded("not a cursor"),
        encoded("2026-11-01T00:00:00|5"),  # a time without a zone
        encoded("2026-11-01T00:00:00+00:00|x"),
        encoded("2026-11-01T00:00:00+00:00|-3"),
        encoded("2026-11-01T00:00:00+00:00|5|extra"),
    ],
)
def test_a_malformed_cursor_is_refused(org: Org_, cursor: str) -> None:
    with pytest.raises(Invalid) as error:
        list_all(org, cursor)

    assert error.value.code == "invalid_cursor"


def test_shops_are_listed_for_the_org_only(org: Org_) -> None:
    make_org("Other Org")
    with session_scope() as session:
        session.add(Shop(org_id=org.org_id, name="A Second Shop", area="Test Area"))

    with session_scope(read_only=True) as session:
        names = [shop.name for shop in shops.list_shops(session, org.org_id)]

    assert names == ["A Second Shop", "Test Shop"]


# --- helpers used above -----------------------------------------------------------------------


def segment_rows(visit_id: str) -> list[Segment]:
    with session_scope(read_only=True) as session:
        return list(
            session.scalars(
                select(Segment).where(Segment.visit_id == visit_id).order_by(Segment.seq)
            )
        )


def abandon(org: Org_, visit_id: str) -> None:
    with session_scope() as session:
        visit = visits.lock_visit(session, org.org_id, visit_id)
        visits.set_status(visit, VisitStatus.FAILED, reason="Nothing was uploaded for 12 hours.")
        visit.abandoned_at = utc_now()


# --- found by review --------------------------------------------------------------------------


def test_lock_visit_reads_the_row_again_even_if_the_session_already_had_it(org: Org_) -> None:
    visit_id = new_visit(org)
    with session_scope() as session:
        # Kept in a variable: the session's identity map holds objects only weakly, and a
        # request that read the visit earlier holds it too.
        earlier = visits.get_visit(session, org.org_id, visit_id)
        assert earlier.status == "recording"
        move_to(org, visit_id, VisitStatus.FAILED, reason="Stopped.")

        locked = visits.lock_visit(session, org.org_id, visit_id)

        assert locked is earlier
        assert locked.status == "failed"


def test_the_same_segment_again_after_complete_is_still_harmless(org: Org_) -> None:
    visit_id = new_visit(org)
    upload(org, visit_id, 0, b"the bytes")
    complete(org, visit_id, last_seq=0)

    assert upload(org, visit_id, 0, b"the bytes") is False


def test_different_bytes_after_complete_are_a_conflict(org: Org_) -> None:
    visit_id = new_visit(org)
    upload(org, visit_id, 0, b"the bytes")
    complete(org, visit_id, last_seq=0)

    with pytest.raises(Conflict) as error:
        upload(org, visit_id, 0, b"other bytes")

    assert error.value.code == "segment_conflict"


def test_a_new_segment_after_complete_is_refused(org: Org_) -> None:
    visit_id = new_visit(org)
    upload(org, visit_id, 0)
    complete(org, visit_id, last_seq=0)

    with pytest.raises(Conflict) as error:
        upload(org, visit_id, 1, b"late")

    assert error.value.code == "visit_not_recording"


def mark_do_not_record(org: Org_) -> None:
    with session_scope() as session:
        shop = session.get(Shop, org.shop_id)
        assert shop is not None
        shop.do_not_record = True


def test_a_conversation_takes_no_more_audio_once_its_shop_says_no(org: Org_) -> None:
    visit_id = new_visit(org)
    upload(org, visit_id, 0)
    mark_do_not_record(org)

    with pytest.raises(Conflict) as segment_error:
        upload(org, visit_id, 1, b"more")
    with pytest.raises(Conflict) as complete_error:
        complete(org, visit_id, last_seq=0)

    assert segment_error.value.code == complete_error.value.code == "shop_do_not_record"


def test_a_notes_visit_at_a_do_not_record_shop_still_takes_audio(org: Org_) -> None:
    visit_id = new_visit(org, kind=VisitKind.NOTES)
    mark_do_not_record(org)

    assert upload(org, visit_id, 0) is True
    assert complete(org, visit_id, last_seq=0).status == "uploaded"


def test_staging_a_segment_holds_no_lock_on_the_visit(
    org: Org_, monkeypatch: pytest.MonkeyPatch
) -> None:
    visit_id = new_visit(org)
    staging, release = threading.Event(), threading.Event()
    real_stage = storage.stage

    def stage_slowly(body: bytes) -> Path:
        staging.set()
        release.wait(RACE_TIMEOUT_S)
        return real_stage(body)

    monkeypatch.setattr(storage, "stage", stage_slowly)
    thread = threading.Thread(target=upload, args=(org, visit_id, 0))
    thread.start()
    assert staging.wait(RACE_TIMEOUT_S)
    try:
        # NOWAIT fails at once if anyone holds the row: nobody may while bytes are written.
        with session_scope() as other:
            other.scalars(
                select(Visit).where(Visit.id == visit_id).with_for_update(nowait=True)
            ).one()
    finally:
        release.set()
        thread.join(RACE_TIMEOUT_S)

    assert [row.seq for row in segment_rows(visit_id)] == [0]


def test_a_replay_puts_back_a_segment_file_that_went_missing(org: Org_) -> None:
    visit_id = new_visit(org)
    upload(org, visit_id, 0, b"the bytes")
    storage.segment_path(visit_id, 0).unlink()

    assert upload(org, visit_id, 0, b"the bytes") is False

    assert storage.segment_path(visit_id, 0).read_bytes() == b"the bytes"


def test_a_cleanup_that_fails_doesnt_hide_the_real_error(
    org: Org_, monkeypatch: pytest.MonkeyPatch
) -> None:
    visit_id = new_visit(org)
    upload(org, visit_id, 0, b"original")

    def held_by_a_scanner(path: Path) -> None:
        raise PermissionError("the file is in use")

    monkeypatch.setattr(storage, "discard", held_by_a_scanner)

    with pytest.raises(Conflict) as error:
        upload(org, visit_id, 0, b"different")

    assert error.value.code == "segment_conflict"


def test_a_status_change_stamps_updated_at_at_once(org: Org_) -> None:
    visit_id = new_visit(org)
    with session_scope() as session:
        visit = visits.lock_visit(session, org.org_id, visit_id)
        before = visit.updated_at

        visits.set_status(visit, VisitStatus.FAILED, reason="Stopped.")

        # Before any flush: a route reports the visit before the commit flushes it.
        assert visit.updated_at > before
    assert load(visit_id).updated_at == visit.updated_at


# --- fail_processing (batch 4) ----------------------------------------------------------------

AFTER_UPLOAD = [
    VisitStatus.TRANSCRIBING,
    VisitStatus.TRANSCRIBED,
    VisitStatus.EXTRACTING,
    VisitStatus.REVIEW,
    VisitStatus.CONFIRMED,
]
PROCESSING = [
    VisitStatus.UPLOADED,
    VisitStatus.TRANSCRIBING,
    VisitStatus.TRANSCRIBED,
    VisitStatus.EXTRACTING,
]
EARLIER_REASON = "An earlier reason."


def visit_at(org: Org_, status: str) -> str:
    """A visit that got to `status` the way a real one does."""
    visit_id = new_visit(org)
    if status == VisitStatus.FAILED:
        move_to(org, visit_id, VisitStatus.FAILED, reason=EARLIER_REASON)
    elif status != VisitStatus.RECORDING:
        upload(org, visit_id, 0)
        complete(org, visit_id, last_seq=0)
        steps = AFTER_UPLOAD[: AFTER_UPLOAD.index(status) + 1] if status in AFTER_UPLOAD else []
        move_to(org, visit_id, *steps)
    assert load(visit_id).status == status
    return visit_id


def fail_processing(visit_id: str, reason: str = "Processing stopped.") -> bool:
    with session_scope() as session:
        return visits.fail_processing(session, visit_id, reason)


@pytest.mark.parametrize("status", PROCESSING)
def test_fail_processing_fails_a_visit_that_is_processing(org: Org_, status: str) -> None:
    visit_id = visit_at(org, status)

    assert fail_processing(visit_id, "The recording can't be read.") is True

    visit = load(visit_id)
    assert visit.status == VisitStatus.FAILED
    assert visit.failure_reason == "The recording can't be read."


@pytest.mark.parametrize(
    "status",
    [VisitStatus.RECORDING, VisitStatus.REVIEW, VisitStatus.CONFIRMED, VisitStatus.FAILED],
)
def test_fail_processing_leaves_a_visit_that_isnt_processing_alone(org: Org_, status: str) -> None:
    visit_id = visit_at(org, status)
    before = load(visit_id)

    assert fail_processing(visit_id) is False

    after = load(visit_id)
    assert (after.status, after.failure_reason) == (before.status, before.failure_reason)


@pytest.mark.parametrize(
    ("state", "processing"),
    [
        (JobState.QUEUED, True),  # a retry is queued again
        (JobState.RUNNING, True),
        (JobState.DONE, False),
        (JobState.DEAD, False),
    ],
)
def test_a_visit_is_processing_while_it_has_an_active_job(
    org: Org_, state: str, processing: bool
) -> None:
    visit_id, idle_id = visit_at(org, VisitStatus.UPLOADED), new_visit(org)
    claimed = {"locked_by": "w", "locked_at": utc_now()} if state == JobState.RUNNING else {}
    with session_scope() as session:
        session.execute(update(Job).where(Job.visit_id == visit_id).values(state=state, **claimed))

    with session_scope(read_only=True) as session:
        found = visits.processing_visit_ids(session, [visit_id, idle_id])

    assert found == ({visit_id} if processing else set())


def test_fail_processing_of_a_missing_visit_does_nothing() -> None:
    assert fail_processing(new_id()) is False


def test_fail_processing_waits_for_a_change_to_the_same_visit(org: Org_) -> None:
    visit_id = visit_at(org, VisitStatus.EXTRACTING)
    results: list[bool] = []

    def fail_now() -> None:
        results.append(fail_processing(visit_id))

    with session_scope() as session:
        visit = visits.lock_visit(session, org.org_id, visit_id)
        thread = threading.Thread(target=fail_now)
        thread.start()
        thread.join(BLOCKED_S)
        assert thread.is_alive()
        visits.set_status(visit, VisitStatus.REVIEW)
    thread.join(RACE_TIMEOUT_S)

    # It read the visit again under the lock, saw review, and left it.
    assert results == [False]
    assert load(visit_id).status == VisitStatus.REVIEW


# --- fail_abandoned_uploads (batch 4) ---------------------------------------------------------

QUIET_MS = ABANDONED_UPLOAD_MS + 60_000


def quiet_for(visit_id: str, ms: int) -> None:
    """Moves the visit's last change and its segments' arrival ms into the past (Postgres clock)."""
    ago = func.now() - timedelta(milliseconds=ms)
    with session_scope() as session:
        session.execute(update(Visit).where(Visit.id == visit_id).values(updated_at=ago))
        session.execute(update(Segment).where(Segment.visit_id == visit_id).values(received_at=ago))


def fail_abandoned() -> int:
    with session_scope() as session:
        return visits.fail_abandoned_uploads(session)


@pytest.mark.parametrize("client", [VisitClient.WEB, VisitClient.UPLOAD])
def test_a_browser_visit_quiet_for_too_long_is_failed_as_abandoned(org: Org_, client: str) -> None:
    visit_id = new_visit(org, client=client)
    upload(org, visit_id, 0)
    quiet_for(visit_id, QUIET_MS)

    assert fail_abandoned() == 1

    visit = load(visit_id)
    assert visit.status == VisitStatus.FAILED
    assert visit.failure_reason == visits.ABANDONED_REASON
    assert visit.abandoned_at is not None


def test_a_visit_created_long_ago_with_nothing_uploaded_is_failed(org: Org_) -> None:
    visit_id = new_visit(org)
    quiet_for(visit_id, QUIET_MS)

    assert fail_abandoned() == 1


def test_a_visit_whose_last_segment_is_recent_is_left_alone(org: Org_) -> None:
    visit_id = new_visit(org)
    upload(org, visit_id, 0)
    quiet_for(visit_id, QUIET_MS)
    upload(org, visit_id, 1)  # just now

    assert fail_abandoned() == 0
    assert load(visit_id).status == VisitStatus.RECORDING


def test_a_visit_quiet_for_less_than_the_limit_is_left_alone(org: Org_) -> None:
    visit_id = new_visit(org)
    upload(org, visit_id, 0)
    quiet_for(visit_id, ABANDONED_UPLOAD_MS - 60_000)

    assert fail_abandoned() == 0


def test_a_phone_visit_or_one_that_isnt_recording_is_left_alone(org: Org_) -> None:
    phone = new_visit(org, client=VisitClient.PHONE)
    uploaded = visit_at(org, VisitStatus.UPLOADED)
    for visit_id in (phone, uploaded):
        quiet_for(visit_id, QUIET_MS)

    assert fail_abandoned() == 0


@pytest.mark.parametrize("comes_back_with", ["segment", "complete"])
def test_an_abandoned_visit_comes_back_with_its_next_segment_or_complete(
    org: Org_, comes_back_with: str
) -> None:
    visit_id = new_visit(org)
    upload(org, visit_id, 0)
    quiet_for(visit_id, QUIET_MS)
    assert fail_abandoned() == 1

    if comes_back_with == "segment":
        upload(org, visit_id, 1)
        expected = VisitStatus.RECORDING
    else:
        complete(org, visit_id, last_seq=0)
        expected = VisitStatus.UPLOADED

    visit = load(visit_id)
    assert (visit.status, visit.abandoned_at, visit.failure_reason) == (expected, None, None)


def test_a_visit_revived_by_an_old_segment_isnt_failed_again_at_once(org: Org_) -> None:
    visit_id = new_visit(org)
    upload(org, visit_id, 0)
    quiet_for(visit_id, QUIET_MS)
    assert fail_abandoned() == 1

    upload(org, visit_id, 0)  # the same segment again: it revives the visit, and arrived long ago

    assert fail_abandoned() == 0
    assert load(visit_id).status == VisitStatus.RECORDING


def test_a_visit_another_transaction_holds_is_skipped_not_waited_for(org: Org_) -> None:
    visit_id = new_visit(org)
    quiet_for(visit_id, QUIET_MS)

    with session_scope() as holder:
        visits.lock_visit(holder, org.org_id, visit_id)
        started = time.monotonic()
        assert fail_abandoned() == 0
        waited_s = time.monotonic() - started

    assert waited_s < LOCK_TIMEOUT_MS / 1000 / 2
    assert fail_abandoned() == 1


def test_a_segment_that_arrived_after_the_visit_was_picked_is_seen_under_the_lock(
    org: Org_, monkeypatch: pytest.MonkeyPatch
) -> None:
    visit_id = new_visit(org)
    upload(org, visit_id, 0)
    quiet_for(visit_id, QUIET_MS)
    real_quiet_visits = visits._quiet_visits

    def then_a_segment_lands(session: Session, quiet_since: object) -> list[Visit]:
        picked = real_quiet_visits(session, quiet_since)  # type: ignore[arg-type]
        with session_scope() as other:
            other.execute(
                update(Segment).where(Segment.visit_id == visit_id).values(received_at=func.now())
            )
        return picked

    monkeypatch.setattr(visits, "_quiet_visits", then_a_segment_lands)

    assert fail_abandoned() == 0
    assert load(visit_id).status == VisitStatus.RECORDING
