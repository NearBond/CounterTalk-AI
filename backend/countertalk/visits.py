"""Visits: creating them, their status, their segments and complete (BRIEF §2, §6, §7).

Every write that depends on a visit's state starts with lock_visit(), so two requests for the
same visit run one after the other and a check can't be split from its write (§2.3). Segment
bytes are staged before any query, so writing them never holds that lock (§2.6, §9.8).
"""

import base64
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from countertalk import storage
from countertalk.errors import Conflict, Invalid, NotFound, TooLarge
from countertalk.limits import MAX_SEGMENT_BYTES, VISITS_PAGE_SIZE
from countertalk.models import (
    Segment,
    Shop,
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


def _find_visit(session: Session, org_id: str, visit_id: str, *, for_update: bool) -> Visit:
    """One visit of this org. Another org's visit is 404, never 403 (§2.10)."""
    found = parse_id(visit_id)
    visit = None
    if found is not None:
        query = select(Visit).where(Visit.id == found, Visit.org_id == org_id)
        if for_update:
            query = query.with_for_update()
        # Read the row as it is now, even if this session loaded the visit before the lock.
        visit = session.scalars(query.execution_options(populate_existing=True)).one_or_none()
    if visit is None:
        raise NotFound("visit_not_found", "There's no such visit")
    return visit


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


def _refuse_if_shop_said_no(shop: Shop, kind: str) -> None:
    # Enforced here, not only in the UI (§7.4). Notes are the rep's own summary, so they stay.
    if shop.do_not_record and kind == VisitKind.CONVERSATION:
        raise Conflict("shop_do_not_record", "This shop said no to recording; record notes instead")


def create_visit(
    session: Session, org_id: str, rep_id: str, shop_id: str, kind: str, client: str
) -> Visit:
    if kind not in VisitKind or client not in VisitClient:
        raise Invalid("invalid_visit", "That isn't a kind or client of visit the app knows")
    shop = _shop_for_sharing(session, org_id, shop_id)
    if shop is None:
        raise NotFound("shop_not_found", "There's no such shop")
    _refuse_if_shop_said_no(shop, kind)
    visit = Visit(org_id=org_id, rep_id=rep_id, shop_id=shop.id, kind=kind, client=client)
    session.add(visit)
    session.flush()
    return visit


def _check_visit_takes_audio(session: Session, visit: Visit) -> None:
    shop = _shop_for_sharing(session, visit.org_id, visit.shop_id)
    assert shop is not None  # the composite key keeps a visit's shop in its org
    _refuse_if_shop_said_no(shop, visit.kind)


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


def stage_segment(seq: int, body: bytes, sha256: str) -> StagedSegment:
    """Checks the segment and writes it to a staging file, before any query (§2.6, §9.8)."""
    storage.check_seq(seq)
    if not body:
        raise Invalid("empty_segment", "The segment is empty")
    if len(body) > MAX_SEGMENT_BYTES:
        raise TooLarge("segment_too_large", f"A segment can be at most {MAX_SEGMENT_BYTES} bytes")
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
    """Checks the visit has exactly segments 0..last_seq, then marks it uploaded (§9.6)."""
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
    return visit


def list_visits(
    session: Session, org_id: str, cursor: str | None
) -> tuple[list[Visit], str | None]:
    """A page of the org's visits, newest first, and the cursor for the next page, if any."""
    query = select(Visit).where(Visit.org_id == org_id)
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
