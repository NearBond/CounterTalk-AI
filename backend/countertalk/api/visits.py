"""/api/visits: create, list and read visits, upload their segments, complete them, read
their transcript and audio, reprocess them (batch 13), and review them: read the review, add a
finding or a note, and confirm (batch 14; /api/insights edits one insight)."""

import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from starlette.types import Message, Receive, Scope, Send

from countertalk import access, extraction, reprocess, review, storage, transcription, users, visits
from countertalk.api.deps import CurrentUser
from countertalk.api.insights import (
    ChoiceOut,
    InsightOut,
    NewInsight,
    NewNote,
    NoteOut,
    OrderItemOut,
    ReportOut,
    TaskOut,
    insight_out,
)
from countertalk.api.issues import TeamOut
from countertalk.db import DbSession, session_scope
from countertalk.errors import Invalid, LengthRequired
from countertalk.limits import MAX_SEGMENT_BYTES
from countertalk.models import Role, Visit, VisitClient, VisitKind, VisitStatus

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/visits", tags=["visits"])

_SHA256 = re.compile(r"[0-9a-f]{64}")
# ASCII only: str.isdigit() also takes digits like "²", which int() then refuses.
_DIGITS = re.compile(r"[0-9]+")


class VisitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shop_id: UUID
    kind: VisitKind
    client: VisitClient


class VisitOut(BaseModel):
    id: str
    shop_id: str
    rep_id: str
    rep_name: str
    kind: str
    client: str
    status: str
    failure_reason: str | None
    duration_ms: int | None
    # True while it has a queued, running or retrying job: the visit page polls until it's false.
    processing: bool
    completed_at: datetime | None
    # Its recording went under the retention policy (batch 17): ▶ says so instead of playing.
    audio_deleted_at: datetime | None
    created_at: datetime
    updated_at: datetime


class VisitPage(BaseModel):
    items: list[VisitOut]
    next_cursor: str | None


class CompleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Strict: JSON true, "1" or 1.0 aren't a segment number.
    last_seq: StrictInt
    ext: str


class ReprocessIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # "from" in JSON: a Python keyword, so the field has another name.
    start: reprocess.From = Field(alias="from")


class ReviewOut(BaseModel):
    """Everything the review page shows, in one call (batch 14)."""

    visit: VisitOut
    report: ReportOut | None
    tasks: list[TaskOut]
    # Read-only for now: the rep can't edit them yet.
    order_items: list[OrderItemOut]
    insights: list[InsightOut]
    notes: list[NoteOut]
    # Each speaker label's saved role ("c0:A": "rep"), for the add-a-finding picker.
    speaker_roles: dict[str, str]
    # The playbook's keys and labels, for the page's pickers.
    kinds: list[ChoiceOut]
    categories: list[ChoiceOut]
    severities: list[ChoiceOut]
    # The org's teams, for the rep's team picker (batch F1).
    teams: list[TeamOut]


class TurnOut(BaseModel):
    turn_id: int
    speaker_label: str
    start_ms: int
    end_ms: int
    text: str


class TranscriptOut(BaseModel):
    id: str
    visit_id: str
    provider: str
    model: str
    language_code: str | None
    created_at: datetime
    turns: list[TurnOut]


class SegmentOut(BaseModel):
    seq: int
    bytes: int
    sha256: str
    received_at: datetime


async def _staged_segment(
    request: Request, visit_id: UUID, seq: int, user: CurrentUser
) -> AsyncIterator[visits.StagedSegment]:
    """Reads the body and stages it before any query, so writing it holds no lock (§2.6, §9.8).

    `visit_id` and `user` are here so that a malformed ID, or no current user, stops the
    request before a byte of the body is read; the route shares the same user lookup. The
    headers are checked next, so a body that's too big is never read, and the body is read with
    a hard cap anyway, whatever Content-Length said.
    """
    storage.check_seq(seq)
    # Only the visit's rep uploads to it, checked before a byte is read.
    await run_in_threadpool(_may_change, user, str(visit_id))
    length = request.headers.get("content-length")
    if length is None:
        raise LengthRequired("length_required", "Send the segment with a Content-Length header")
    if not _DIGITS.fullmatch(length):
        raise Invalid("invalid_content_length", "Content-Length must be a number of bytes")
    if int(length) > MAX_SEGMENT_BYTES:
        raise visits.segment_too_large()
    sha256 = request.headers.get("x-content-sha256")
    if sha256 is None:
        raise Invalid("checksum_required", "Send the segment's SHA-256 in X-Content-SHA256")
    if not _SHA256.fullmatch(sha256):
        raise Invalid("invalid_checksum", "X-Content-SHA256 must be 64 lowercase hex characters")
    body = bytearray()
    try:
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_SEGMENT_BYTES:
                raise visits.segment_too_large()
    except ClientDisconnect:
        # A phone or a tab losing its connection: the upload queue retries, so it's no error.
        log.info("segment upload broke off visit=%s seq=%d", visit_id, seq)
        raise Invalid("upload_interrupted", "The upload broke off before it finished") from None
    staged = await run_in_threadpool(visits.stage_segment, seq, body, sha256)
    try:
        yield staged
    finally:
        # Off the event loop, like the staging itself.
        await run_in_threadpool(staged.__exit__, None, None, None)


def _may_change(user: users.CurrentUser, visit_id: str) -> None:
    with session_scope(read_only=True) as session:
        access.visit(session, user, visit_id, change=True)


StagedSegment = Annotated[visits.StagedSegment, Depends(_staged_segment)]


_VISIT_COLUMNS = [name for name in VisitOut.model_fields if name not in ("processing", "rep_name")]


def _visit_out(visit: Visit, processing: set[str], names: dict[str, str]) -> VisitOut:
    """`processing` is the set of visit IDs with an active job (visits.processing_visit_ids),
    and `names` the reps' names by ID (visits.rep_names)."""
    columns = {name: getattr(visit, name) for name in _VISIT_COLUMNS}
    return VisitOut(**columns, processing=visit.id in processing, rep_name=names[visit.rep_id])


def _one_visit_out(session: Session, visit: Visit) -> VisitOut:
    processing = visits.processing_visit_ids(session, [visit.id])
    return _visit_out(visit, processing, visits.rep_names(session, [visit]))


@router.post("", status_code=status.HTTP_201_CREATED)
def create_visit(body: VisitIn, user: CurrentUser, session: DbSession) -> VisitOut:
    access.only(user, Role.REP)  # reps record
    visit = visits.create_visit(
        session, user.org_id, user.id, str(body.shop_id), body.kind.value, body.client.value
    )
    return _visit_out(visit, set(), visits.rep_names(session, [visit]))  # no job yet


@router.get("")
def list_visits(
    user: CurrentUser,
    session: DbSession,
    cursor: str | None = None,
    status: VisitStatus | None = None,
) -> VisitPage:
    """Newest first; `status` gives only those (the rep's home's visits in review, batch F3)."""
    rep = access.visits_of(user)
    page, next_cursor = visits.list_visits(session, user.org_id, cursor, rep, status)
    processing = visits.processing_visit_ids(session, [visit.id for visit in page])
    names = visits.rep_names(session, page)
    return VisitPage(
        items=[_visit_out(visit, processing, names) for visit in page], next_cursor=next_cursor
    )


@router.get("/{visit_id}")
def get_visit(visit_id: UUID, user: CurrentUser, session: DbSession) -> VisitOut:
    access.visit(session, user, str(visit_id))
    return _one_visit_out(session, visits.get_visit(session, user.org_id, str(visit_id)))


@router.get("/{visit_id}/transcript")
def get_transcript(visit_id: UUID, user: CurrentUser, session: DbSession) -> TranscriptOut:
    """404 transcript_not_found until the visit is transcribed."""
    access.visit(session, user, str(visit_id))
    found = transcription.current_transcript(session, user.org_id, str(visit_id))
    t = found.transcript
    return TranscriptOut(
        id=t.id,
        visit_id=t.visit_id,
        provider=t.provider,
        model=t.model,
        language_code=t.language_code,
        created_at=t.created_at,
        turns=[TurnOut.model_validate(turn, from_attributes=True) for turn in found.turns],
    )


class RecordingResponse(FileResponse):
    """A FileResponse whose file retention may delete between the check and the send (batch
    17): if it's gone before anything is sent, the same 404 as a visit whose audio was
    deleted, not a 500."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        started = False

        async def sending(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await super().__call__(scope, receive, sending)
        except (RuntimeError, FileNotFoundError):
            if started or Path(self.path).exists():
                raise
            gone = visits.audio_deleted()
            body = {"error": {"code": gone.code, "message": gone.message}}
            await JSONResponse(body, status_code=404)(scope, receive, send)


@router.get("/{visit_id}/audio", response_class=FileResponse)
def get_audio(visit_id: UUID, user: CurrentUser, session: DbSession) -> FileResponse:
    """playback.m4a, with Range requests so a player can seek (Starlette's FileResponse)."""
    access.visit(session, user, str(visit_id))
    path = visits.playback_file(session, user.org_id, str(visit_id))
    # A customer's recording: never kept by a shared cache, and checked again before reuse.
    return RecordingResponse(
        path, media_type="audio/mp4", headers={"Cache-Control": "private, no-cache"}
    )


@router.put("/{visit_id}/segments/{seq}")
def put_segment(
    visit_id: UUID,
    seq: int,
    staged: StagedSegment,
    user: CurrentUser,
    session: DbSession,
    response: Response,
) -> SegmentOut:
    """201 for a new segment, 200 for the same bytes again (safe to repeat, §2.5)."""
    stored = visits.store_staged_segment(session, user.org_id, str(visit_id), staged)
    response.status_code = status.HTTP_201_CREATED if stored.created else status.HTTP_200_OK
    return SegmentOut(
        seq=stored.seq, bytes=stored.bytes, sha256=stored.sha256, received_at=stored.received_at
    )


@router.post(
    "/{visit_id}/reprocess",
    description=(
        "Runs a visit in review or failed again, before it's confirmed: from extract (the"
        " transcript and roles reused, only extraction paid for) or from transcribe (a new"
        f" transcript). {reprocess.PAYS_AGAIN}; one that failed part-way through transcription"
        " keeps the replies already paid for. 409 for a visit that's confirmed or still being"
        " processed, from extract with no transcript, and from transcribe if its upload never"
        " finished."
    ),
)
def reprocess_visit(
    visit_id: UUID, body: ReprocessIn, user: CurrentUser, session: DbSession
) -> VisitOut:
    access.visit(session, user, str(visit_id), change=True)
    visit = reprocess.reprocess(session, user.org_id, str(visit_id), body.start)
    return _one_visit_out(session, visit)


@router.get("/{visit_id}/review")
def get_review(visit_id: UUID, user: CurrentUser, session: DbSession) -> ReviewOut:
    part = access.visit(session, user, str(visit_id))
    pack = partial(extraction.pack_for, user.org_id, session=session)
    found = review.review_of(session, pack, user.org_id, str(visit_id))
    if part is not None:
        found = review.team_part(found, part)

    def choices(items: list[review.Choice]) -> list[ChoiceOut]:
        return [ChoiceOut(key=item.key, label=item.label) for item in items]

    return ReviewOut(
        visit=_one_visit_out(session, found.visit),
        report=ReportOut.model_validate(found.report, from_attributes=True)
        if found.report is not None
        else None,
        tasks=[TaskOut.model_validate(task, from_attributes=True) for task in found.tasks],
        order_items=[
            OrderItemOut.model_validate(item, from_attributes=True) for item in found.order_items
        ],
        insights=[insight_out(view) for view in found.insights],
        notes=[NoteOut.model_validate(note, from_attributes=True) for note in found.notes],
        speaker_roles=found.speaker_roles,
        kinds=choices(found.kinds),
        categories=choices(found.categories),
        severities=choices(found.severities),
        teams=[TeamOut(id=team.id, key=team.key, name=team.name) for team in found.teams],
    )


@router.post("/{visit_id}/insights", status_code=status.HTTP_201_CREATED)
def add_insight(
    visit_id: UUID, body: NewInsight, user: CurrentUser, session: DbSession
) -> InsightOut:
    """A finding the AI missed: the turn becomes its quote, exact and playable. 404 for a turn
    that isn't in the visit's transcript. The same ID again returns the one added."""
    access.visit(session, user, str(visit_id), change=True)
    pack = extraction.pack_for(user.org_id, session=session)
    fields = body.model_dump(exclude={"id"})
    found = review.add_insight(
        session, pack, user.org_id, user.id, str(visit_id), insight_id=str(body.id), **fields
    )
    return insight_out(review.review_view(session, pack, found))


@router.post("/{visit_id}/notes", status_code=status.HTTP_201_CREATED)
def add_note(visit_id: UUID, body: NewNote, user: CurrentUser, session: DbSession) -> NoteOut:
    """For the visit's record only: never routed, never shown to teams."""
    access.visit(session, user, str(visit_id), change=True)
    note = review.add_note(session, user.org_id, user.id, str(visit_id), str(body.id), body.text)
    return NoteOut.model_validate(note, from_attributes=True)


@router.post("/{visit_id}/confirm")
def confirm_visit(visit_id: UUID, user: CurrentUser, session: DbSession) -> VisitOut:
    """Review to confirmed, and locked against edits; confirming again returns it as it is."""
    access.visit(session, user, str(visit_id), change=True)
    pack = partial(extraction.pack_for, user.org_id, session=session)
    visit = review.confirm(session, pack, user.org_id, user.id, str(visit_id))
    return _one_visit_out(session, visit)


@router.post("/{visit_id}/complete")
def complete_visit(
    visit_id: UUID, body: CompleteIn, user: CurrentUser, session: DbSession
) -> VisitOut:
    access.visit(session, user, str(visit_id), change=True)
    visit = visits.complete_visit(session, user.org_id, str(visit_id), body.last_seq, body.ext)
    return _one_visit_out(session, visit)
