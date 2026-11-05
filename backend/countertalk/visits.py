"""Visits: creating them, their status, their segments and complete (BRIEF §2, §6, §7).

Every write that depends on a visit's state starts with lock_visit(), so two requests for the
same visit run one after the other and a check can't be split from its write (§2.3). Segment
bytes are staged before any query, so writing them never holds that lock (§2.6, §9.8).
"""

import base64
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType

from sqlalchemy import ColumnElement, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from countertalk import consent, jobs, rows, storage
from countertalk.errors import Conflict, Invalid, NotFound, TooLarge
from countertalk.limits import ABANDONED_UPLOAD_MS, MAX_SEGMENT_BYTES, VISITS_PAGE_SIZE
from countertalk.models import (
    ACTIVE_JOB_STATES,
    Job,
    JobKind,
    Segment,
    Shop,
    User,
    Visit,
    VisitClient,
    VisitKind,
    VisitStatus,
    parse_id,
    utc_now,
)

log = logging.getLogger(__name__)

S = VisitStatus

# The forward steps, and the two ways back (BRIEF §6), whose conditions set_status checks:
# failed -> recording for an abandoned web or upload visit, and reprocess from review or failed.
# Any status except failed can also become failed.
_STEPS: dict[str, frozenset[str]] = {
    S.RECORDING: frozenset({S.UPLOADED}),
    S.UPLOADED: frozenset({S.TRANSCRIBING}),
    S.TRANSCRIBING: frozenset({S.TRANSCRIBED}),
    S.TRANSCRIBED: frozenset({S.EXTRACTING}),
    S.EXTRACTING: frozenset({S.REVIEW}),
    S.REVIEW: frozenset({S.CONFIRMED, S.UPLOADED, S.TRANSCRIBED}),
    S.CONFIRMED: frozenset(),
    S.FAILED: frozenset({S.RECORDING, S.UPLOADED, S.TRANSCRIBED}),
}
_REVIVABLE_CLIENTS = frozenset({VisitClient.WEB, VisitClient.UPLOAD})


def set_status(visit: Visit, new_status: str, *, reason: str | None = None) -> None:
    """The only way a visit's status changes (§2.5). Call it under lock_visit."""
    old = visit.status
    if old == S.CONFIRMED:
        # Final (batch 17's CHECK): confirmed_at is when its audio's retention period started.
        raise Conflict("invalid_status_change", "A confirmed visit can't change")
    if not (new_status == S.FAILED and old != S.FAILED or new_status in _STEPS[old]):
        raise Conflict("invalid_status_change", f"A visit can't go from {old} to {new_status}")
    if new_status == S.FAILED and not reason:
        raise ValueError("a failed visit needs a reason the rep can read")
    if new_status == S.RECORDING and not (
        visit.client in _REVIVABLE_CLIENTS and visit.abandoned_at is not None
    ):
        raise Conflict("invalid_status_change", "Only an abandoned browser visit comes back")
    if new_status == S.UPLOADED and old != S.RECORDING and visit.completed_at is None:
        raise Conflict("invalid_status_change", "Its upload never finished, so it can't redo it")
    # To transcribed from review or failed (reprocess), the caller checks there's a transcript
    # (batch 13).
    visit.status = new_status
    # Now, not at the flush: a route reports the visit before its commit flushes it.
    visit.updated_at = utc_now()
    if new_status == S.CONFIRMED:
        visit.confirmed_at = visit.updated_at  # when its audio's retention period starts
    if new_status == S.FAILED:
        visit.failure_reason = reason
    elif old == S.FAILED:
        visit.failure_reason = None
    if new_status == S.RECORDING:
        visit.abandoned_at = None


def _revive_if_abandoned(visit: Visit) -> None:
    # A browser tab that went quiet and came back (§9.7).
    if (
        visit.status == S.FAILED
        and visit.abandoned_at is not None
        and visit.client in _REVIVABLE_CLIENTS
    ):
        set_status(visit, S.RECORDING)


_PROCESSING = frozenset({S.UPLOADED, S.TRANSCRIBING, S.TRANSCRIBED, S.EXTRACTING})


def lock_for_work(session: Session, visit_id: str) -> Visit | None:
    """lock_visit for the worker: by ID alone, because the worker works across orgs."""
    query = select(Visit).where(Visit.id == visit_id).with_for_update()
    return session.scalars(query.execution_options(populate_existing=True)).one_or_none()


def fail_processing(session: Session, visit_id: str, reason: str) -> bool:
    """Fails a visit whose background work gave up (§9.1), with a reason the rep can read.

    Under the visit's lock, and only while it's being processed: a visit that's already failed,
    in review or confirmed is left as it is. True if it was failed.
    """
    visit = lock_for_work(session, visit_id)
    if visit is None or visit.status not in _PROCESSING:
        return False
    log.info("visit failed visit=%s status=%s", visit.id, visit.status)
    set_status(visit, S.FAILED, reason=reason)
    return True


def add_stt_cost(session: Session, visit_id: str, billed_s: int, wall_ms: int) -> None:
    """Adds one paid speech-to-text call to the visit's totals: one statement, so nothing to
    check first, and no lock held beyond it."""
    session.execute(
        update(Visit)
        .where(Visit.id == visit_id)
        .values(
            stt_seconds_billed=Visit.stt_seconds_billed + billed_s,
            stt_wall_ms=Visit.stt_wall_ms + wall_ms,
        )
    )


ABANDONED_REASON = "The recording stopped arriving; it carries on if the recording tab comes back"


def _last_activity() -> ColumnElement:
    """When the visit last changed or last got a segment.

    Those times come from the app's clock and are compared with Postgres's now(): a clock a few
    seconds off doesn't matter against a 12-hour limit.
    """
    last_segment = (
        select(func.max(Segment.received_at)).where(Segment.visit_id == Visit.id).scalar_subquery()
    )
    # updated_at too: a visit revived by a replayed old segment isn't failed again at once.
    return func.greatest(Visit.updated_at, last_segment)


def _quiet_visits(session: Session, quiet_since: ColumnElement) -> list[Visit]:
    """Browser visits still recording with nothing new since then, locked. Held ones are skipped."""
    query = (
        select(Visit)
        .where(
            Visit.status == S.RECORDING,
            Visit.client.in_(_REVIVABLE_CLIENTS),
            _last_activity() < quiet_since,
        )
        .order_by(Visit.seq)
        .with_for_update(skip_locked=True, of=Visit)
    )
    return list(session.scalars(query.execution_options(populate_existing=True)))


def fail_abandoned_uploads(session: Session) -> int:
    """Fails web and upload visits still recording with nothing new for ABANDONED_UPLOAD_MS (§9.1).

    It can be undone (§9.7): abandoned_at is set, so the tab's next segment, or complete, brings
    the visit back. Safe while another worker does the same: a visit another transaction holds
    is skipped, and the quiet is checked again under the lock, so a segment that arrived since
    counts. Returns how many it failed.
    """
    quiet_since = func.now() - timedelta(milliseconds=ABANDONED_UPLOAD_MS)
    picked = _quiet_visits(session, quiet_since)
    if not picked:
        return 0
    # A new statement sees what was committed before the locks were taken.
    still_quiet = set(
        session.scalars(
            select(Visit.id).where(
                Visit.id.in_([visit.id for visit in picked]), _last_activity() < quiet_since
            )
        )
    )
    failed = 0
    for visit in picked:
        if visit.id not in still_quiet:
            continue
        log.info("visit abandoned visit=%s", visit.id)
        set_status(visit, S.FAILED, reason=ABANDONED_REASON)
        visit.abandoned_at = utc_now()
        failed += 1
    return failed


def missing_visit() -> NotFound:
    """A fresh one each time (errors_are_raised_fresh)."""
    return NotFound("visit_not_found", "There's no such visit")


def _find_visit(session: Session, org_id: str, visit_id: str, *, for_update: bool) -> Visit:
    """One visit of this org. Another org's visit is 404, never 403 (§2.10)."""
    return rows.find_one(session, Visit, org_id, visit_id, missing_visit(), for_update=for_update)


def lock_visit(session: Session, org_id: str, visit_id: str) -> Visit:
    """SELECT … FOR UPDATE on one visit of this org: writes to it run one after the other."""
    return _find_visit(session, org_id, visit_id, for_update=True)


def get_visit(session: Session, org_id: str, visit_id: str) -> Visit:
    return _find_visit(session, org_id, visit_id, for_update=False)


def _shop_for_sharing(session: Session, org_id: str, shop_id: str) -> Shop | None:
    # FOR SHARE: a do_not_record being set at the same moment is waited for, not missed.
    found = parse_id(shop_id)
    if found is None:
        return None
    query = select(Shop).where(Shop.id == found, Shop.org_id == org_id).with_for_update(read=True)
    return session.scalars(query.execution_options(populate_existing=True)).one_or_none()


def _refuse_if_shop_said_no(session: Session, shop: Shop, kind: str) -> None:
    # Enforced here, not only in the UI (§7.4), under the shop's FOR SHARE lock, which a consent
    # answer's FOR UPDATE waits for. Notes are the rep's own summary, so they stay.
    if kind != VisitKind.CONVERSATION:
        return
    if shop.do_not_record:
        raise Conflict("shop_do_not_record", "This shop said no to recording; record notes instead")
    if not consent.agreed(session, shop):
        # Batch 17: a yes given before the visit, by the shop's link or on paper.
        raise Conflict(
            "consent_needed",
            "This shop hasn't agreed to recording yet: share its consent link, or mark the paper"
            " form it signed",
        )


def create_visit(
    session: Session, org_id: str, rep_id: str, shop_id: str, kind: str, client: str
) -> Visit:
    if kind not in VisitKind or client not in VisitClient:
        raise Invalid("invalid_visit", "That isn't a kind or client of visit the app knows")
    shop = _shop_for_sharing(session, org_id, shop_id)
    if shop is None:
        raise NotFound("shop_not_found", "There's no such shop")
    _refuse_if_shop_said_no(session, shop, kind)
    visit = Visit(org_id=org_id, rep_id=rep_id, shop_id=shop.id, kind=kind, client=client)
    session.add(visit)
    session.flush()
    return visit


def _check_visit_takes_audio(session: Session, visit: Visit) -> None:
    shop = _shop_for_sharing(session, visit.org_id, visit.shop_id)
    assert shop is not None  # the composite key keeps a visit's shop in its org
    _refuse_if_shop_said_no(session, shop, visit.kind)


@dataclass(frozen=True)
class StagedSegment:
    """A segment's bytes, checked and written to a staging file before any query.

    Use it in a `with` block: on the way out it removes the staging file unless the segment
    was stored, so no failure path leaves one behind (§9.10).
    """

    seq: int
    path: Path
    bytes: int
    sha256: str

    def __enter__(self) -> "StagedSegment":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            storage.discard(self.path)
        except (OSError, ValueError) as error:
            # Never in place of the error that's on its way out; a stray file is only clutter.
            log.warning("staged segment not removed error=%s", type(error).__name__)


@dataclass(frozen=True)
class StoredSegment:
    seq: int
    bytes: int
    sha256: str
    received_at: datetime
    # False for the same bytes sent again, which changed nothing.
    created: bool


def segment_too_large() -> TooLarge:
    return TooLarge("segment_too_large", f"A segment can be at most {MAX_SEGMENT_BYTES} bytes")


def stage_segment(seq: int, body: bytes | bytearray, sha256: str) -> StagedSegment:
    """Checks the segment and writes it to a staging file, before any query (§2.6, §9.8)."""
    storage.check_seq(seq)
    if not body:
        raise Invalid("empty_segment", "The segment is empty")
    if len(body) > MAX_SEGMENT_BYTES:
        raise segment_too_large()
    if hashlib.sha256(body).hexdigest() != sha256:
        raise Invalid("segment_checksum_mismatch", "The checksum doesn't match the uploaded bytes")
    return StagedSegment(seq, storage.stage(body), len(body), sha256)


def _same_segment_again(segment: Segment, staged: StagedSegment, final: Path) -> StoredSegment:
    if (segment.sha256, segment.bytes) != (staged.sha256, staged.bytes):
        raise Conflict(
            "segment_conflict",
            f"Segment {segment.seq} was already uploaded with different bytes",
            details={"seq": segment.seq},
        )
    if not final.is_file():
        # The checksum says the bytes are the same, so a file that went missing goes back.
        storage.move_into_place(staged.path, final)
    return StoredSegment(segment.seq, segment.bytes, segment.sha256, segment.received_at, False)


def store_staged_segment(
    session: Session, org_id: str, visit_id: str, staged: StagedSegment
) -> StoredSegment:
    """Stores a staged segment of a recording visit.

    The same bytes again change nothing, whatever the visit's status (BRIEF §2, §2.5);
    different bytes are a conflict.
    """
    visit = lock_visit(session, org_id, visit_id)
    _revive_if_abandoned(visit)
    _check_visit_takes_audio(session, visit)
    final = storage.segment_path(visit.id, staged.seq)
    if visit.status != S.RECORDING:
        existing = session.get(Segment, (visit.id, staged.seq), populate_existing=True)
        if existing is None:
            raise Conflict("visit_not_recording", "This visit isn't recording any more")
        return _same_segment_again(existing, staged, final)
    table = Segment.__table__
    row = session.execute(
        insert(table)
        .values(
            visit_id=visit.id,
            seq=staged.seq,
            path=storage.relative_to_data_dir(final),
            bytes=staged.bytes,
            sha256=staged.sha256,
            received_at=utc_now(),
        )
        .on_conflict_do_nothing(index_elements=["visit_id", "seq"])
        .returning(table.c.received_at)
    ).first()
    if row is None:
        existing = session.get(Segment, (visit.id, staged.seq), populate_existing=True)
        assert existing is not None  # the insert met it, and the visit's lock is held
        return _same_segment_again(existing, staged, final)
    # After the row, so a failed move rolls the row back.
    storage.move_into_place(staged.path, final)
    return StoredSegment(staged.seq, staged.bytes, staged.sha256, row.received_at, True)


def complete_visit(session: Session, org_id: str, visit_id: str, last_seq: int, ext: str) -> Visit:
    """Checks the visit has exactly segments 0..last_seq, then marks it uploaded (§9.6) and
    queues its transcription, in the same transaction: the visit, then its job (§2.3)."""
    storage.check_ext(ext)
    storage.check_seq(last_seq)
    visit = lock_visit(session, org_id, visit_id)
    _revive_if_abandoned(visit)
    _check_visit_takes_audio(session, visit)
    expected = set(range(last_seq + 1))
    stored = set(session.scalars(select(Segment.seq).where(Segment.visit_id == visit.id)))
    if visit.status != S.RECORDING:
        if visit.completed_at is not None and visit.source_ext == ext and stored == expected:
            # The same complete again, e.g. a retry whose first answer was lost (§2.5).
            return visit
        raise Conflict("visit_not_recording", "This visit isn't recording any more")
    if missing := sorted(expected - stored):
        raise Conflict(
            "segments_missing", "Some segments haven't arrived", details={"missing_seqs": missing}
        )
    if extra := sorted(stored - expected):
        raise Conflict(
            "unexpected_segments",
            "Segments past the last one were uploaded",
            details={"unexpected_seqs": extra},
        )
    visit.source_ext = ext
    visit.completed_at = utc_now()
    set_status(visit, S.UPLOADED)
    jobs.enqueue(session, JobKind.TRANSCRIBE, visit_id=visit.id)
    return visit


def processing_visit_ids(session: Session, visit_ids: list[str]) -> set[str]:
    """Those of these visits with a queued, running or retrying job: one query for a page."""
    query = select(Job.visit_id).where(
        Job.visit_id.in_(visit_ids), Job.state.in_(ACTIVE_JOB_STATES)
    )
    return {str(visit_id) for visit_id in session.scalars(query.distinct())}


def rep_names(session: Session, page: list[Visit]) -> dict[str, str]:
    """The name of each of these visits' reps, by rep ID: one query for a page."""
    query = select(User.id, User.name).where(User.id.in_({visit.rep_id for visit in page}))
    return {rep_id: name for rep_id, name in session.execute(query)}


def audio_deleted() -> NotFound:
    return NotFound("audio_deleted", "Audio deleted under the retention policy")


def playback_file(session: Session, org_id: str, visit_id: str) -> Path:
    """The visit's playback.m4a, once its audio is prepared (batch 6), until retention
    deletes it (batch 17)."""
    visit = get_visit(session, org_id, visit_id)
    if visit.audio_deleted_at is not None:
        raise audio_deleted()
    path = storage.playback_path(visit.id)
    if not path.is_file():
        raise NotFound("audio_not_found", "The recording isn't ready to play yet")
    return path


def list_visits(
    session: Session,
    org_id: str,
    cursor: str | None,
    rep_id: str | None = None,
    status: str | None = None,
) -> tuple[list[Visit], str | None]:
    """A page of the org's visits, or one rep's, of one status if given, newest first, and the
    cursor for the next page, if any."""
    query = select(Visit).where(Visit.org_id == org_id)
    if rep_id is not None:
        query = query.where(Visit.rep_id == rep_id)
    if status is not None:
        query = query.where(Visit.status == status)
    if cursor is not None:
        created_at, seq = _decode_cursor(cursor)
        query = query.where(tuple_(Visit.created_at, Visit.seq) < tuple_(created_at, seq))
    rows = list(
        session.scalars(
            query.order_by(Visit.created_at.desc(), Visit.seq.desc()).limit(VISITS_PAGE_SIZE + 1)
        )
    )
    page = rows[:VISITS_PAGE_SIZE]
    return page, (_encode_cursor(page[-1]) if len(rows) > VISITS_PAGE_SIZE else None)


def _encode_cursor(visit: Visit) -> str:
    text = f"{visit.created_at.isoformat()}|{visit.seq}"
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    invalid = Invalid("invalid_cursor", "That page cursor isn't one this server made")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        text = base64.b64decode(padded, altchars=b"-_", validate=True).decode()
        when, seq = text.split("|")
        created_at, number = datetime.fromisoformat(when), int(seq)
    except ValueError:  # binascii.Error and UnicodeDecodeError are ValueErrors too
        raise invalid from None
    if created_at.utcoffset() is None or number < 1:
        raise invalid
    return created_at, number
