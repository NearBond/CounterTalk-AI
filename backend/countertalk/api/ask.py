"""POST /api/ask (batch 17d): a question across visits, turned into the inbox's filter by Muse
Spark and run by code (countertalk/ask.py). For managers and admins, across the org; a team
member's are their own team's issues, as their inbox is."""

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, StringConstraints

from countertalk import access, ask, limits
from countertalk.api.deps import CurrentUser
from countertalk.api.issues import FindingOut, finding_out
from countertalk.config import get_settings
from countertalk.llm.base import StructuredLlm
from countertalk.llm.client import MetaLlm

router = APIRouter(prefix="/api/ask", tags=["ask"])


def ask_llm() -> StructuredLlm | None:
    """Muse Spark, or None without MODEL_API_KEY (503, and the question isn't counted). Tests
    put a fake in its place."""
    key = get_settings().MODEL_API_KEY.get_secret_value()
    return _client(key) if key else None


@lru_cache(maxsize=1)
def _client(key: str) -> MetaLlm:
    """One client, and its connections, for every question while the key stays the same."""
    return MetaLlm(
        key, timeout_ms=limits.ASK_LLM_TIMEOUT_MS, max_retries=limits.ASK_LLM_MAX_RETRIES
    )


AskLlm = Annotated[StructuredLlm | None, Depends(ask_llm)]
Question = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=limits.ASK_QUESTION_MAX_CHARS
    ),
]


class AskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: Question


class ChipOut(BaseModel):
    """What was understood, read-only: "Category", "Pricing and margins"."""

    name: str
    value: str


class AskOut(BaseModel):
    """The answer. Not understood: why, with no chips and no findings (no query ran)."""

    understood: bool
    reason: str | None
    chips: list[ChipOut]
    # Newest first, at most ASK_FINDINGS_MAX.
    findings: list[FindingOut]


@router.post("")
def ask_question(body: AskIn, user: CurrentUser, llm: AskLlm) -> AskOut:
    """503 llm_not_configured without a key; 429 ask_limit (Retry-After) past the day's
    questions."""
    access.team_work(user)
    found = ask.ask(llm, user, body.question)
    return AskOut(
        understood=found.understood,
        reason=found.reason,
        chips=[ChipOut(name=name, value=value) for name, value in found.chips],
        findings=[finding_out(item) for item in found.findings],
    )
