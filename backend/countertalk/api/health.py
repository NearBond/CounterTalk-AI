from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from countertalk.db import DbSession, database_reachable

router = APIRouter(tags=["health"])


class HealthOut(BaseModel):
    status: Literal["ok", "error"]
    database: Literal["ok", "error"]


@router.get("")
def health(session: DbSession, response: Response) -> HealthOut:
    """Railway's health check: 503 when Postgres can't be reached."""
    if database_reachable(session):
        return HealthOut(status="ok", database="ok")
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthOut(status="error", database="error")
