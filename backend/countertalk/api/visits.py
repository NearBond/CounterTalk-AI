"""/api/visits: create, list and read visits, upload their segments, and complete them."""

import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, StrictInt
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from countertalk import storage, visits
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


def _visit_out(visit: Visit) -> VisitOut:
    return VisitOut.model_validate(visit, from_attributes=True)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_visit(body: VisitIn, user: CurrentUser, session: DbSession) -> VisitOut:
    visit = visits.create_visit(
        session, user.org_id, user.id, str(body.shop_id), body.kind.value, body.client.value
    )
    return _visit_out(visit)


@router.get("")
def list_visits(user: CurrentUser, session: DbSession, cursor: str | None = None) -> VisitPage:
    page, next_cursor = visits.list_visits(session, user.org_id, cursor)
    return VisitPage(items=[_visit_out(visit) for visit in page], next_cursor=next_cursor)


@router.get("/{visit_id}")
def get_visit(visit_id: UUID, user: CurrentUser, session: DbSession) -> VisitOut:
    return _visit_out(visits.get_visit(session, user.org_id, str(visit_id)))


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


@router.post("/{visit_id}/complete")
def complete_visit(
    visit_id: UUID, body: CompleteIn, user: CurrentUser, session: DbSession
) -> VisitOut:
    visit = visits.complete_visit(session, user.org_id, str(visit_id), body.last_seq, body.ext)
    return _visit_out(visit)
