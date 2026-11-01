"""/api/visits: create, list and read visits, upload their segments, complete them, read
their transcript and audio, and reprocess them (batch 13)."""

import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from countertalk import reprocess, storage, transcription, visits
from countertalk.api.deps import CurrentUser
from countertalk.db import DbSession
from countertalk.errors import Invalid, LengthRequired
from countertalk.limits import MAX_SEGMENT_BYTES
from countertalk.models import Visit, VisitClient, VisitKind

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
    kind: str
    client: str
    status: str
    failure_reason: str | None
    duration_ms: int | None
    # True while it has a queued, running or retrying job: the visit page polls until it's false.
    processing: bool
    completed_at: datetime | None
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


StagedSegment = Annotated[visits.StagedSegment, Depends(_staged_segment)]


_VISIT_COLUMNS = [name for name in VisitOut.model_fields if name != "processing"]


def _visit_out(visit: Visit, processing: set[str]) -> VisitOut:
    """`processing` is the set of visit IDs with an active job (visits.processing_visit_ids)."""
    columns = {name: getattr(visit, name) for name in _VISIT_COLUMNS}
    return VisitOut(**columns, processing=visit.id in processing)


def _one_visit_out(session: Session, visit: Visit) -> VisitOut:
    return _visit_out(visit, visits.processing_visit_ids(session, [visit.id]))


@router.post("", status_code=status.HTTP_201_CREATED)
def create_visit(body: VisitIn, user: CurrentUser, session: DbSession) -> VisitOut:
    visit = visits.create_visit(
        session, user.org_id, user.id, str(body.shop_id), body.kind.value, body.client.value
    )
    return _visit_out(visit, set())  # a new visit has no job yet


@router.get("")
def list_visits(user: CurrentUser, session: DbSession, cursor: str | None = None) -> VisitPage:
    page, next_cursor = visits.list_visits(session, user.org_id, cursor)
    processing = visits.processing_visit_ids(session, [visit.id for visit in page])
    return VisitPage(
        items=[_visit_out(visit, processing) for visit in page], next_cursor=next_cursor
    )


@router.get("/{visit_id}")
def get_visit(visit_id: UUID, user: CurrentUser, session: DbSession) -> VisitOut:
    return _one_visit_out(session, visits.get_visit(session, user.org_id, str(visit_id)))


@router.get("/{visit_id}/transcript")
def get_transcript(visit_id: UUID, user: CurrentUser, session: DbSession) -> TranscriptOut:
    """404 transcript_not_found until the visit is transcribed."""
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


@router.get("/{visit_id}/audio", response_class=FileResponse)
def get_audio(visit_id: UUID, user: CurrentUser, session: DbSession) -> FileResponse:
    """playback.m4a, with Range requests so a player can seek (Starlette's FileResponse)."""
    path = visits.playback_file(session, user.org_id, str(visit_id))
    # A customer's recording: never kept by a shared cache, and checked again before reuse.
    return FileResponse(
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
    visit = reprocess.reprocess(session, user.org_id, str(visit_id), body.start)
    return _one_visit_out(session, visit)


@router.post("/{visit_id}/complete")
def complete_visit(
    visit_id: UUID, body: CompleteIn, user: CurrentUser, session: DbSession
) -> VisitOut:
    visit = visits.complete_visit(session, user.org_id, str(visit_id), body.last_seq, body.ext)
    return _one_visit_out(session, visit)
