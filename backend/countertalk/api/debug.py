"""/api/debug: test hooks, included only when DEBUG=1 (main.py)."""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from countertalk import jobs
from countertalk.db import DbSession

router = APIRouter(prefix="/api/debug", tags=["debug"])


class DebugJobIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["ping", "sleep"]


class DebugJobOut(BaseModel):
    id: str
    kind: str


@router.post("/jobs", status_code=201)
def queue_test_job(body: DebugJobIn, session: DbSession) -> DebugJobOut:
    job_id = jobs.enqueue(session, body.kind)
    assert job_id is not None  # jobs without a visit are never merged
    return DebugJobOut(id=job_id, kind=body.kind)
