"""/api/consent/<token>: a shop's consent page, public, with no sign-in (batch 17).

The token is the whole key: an unknown or malformed one gets the same 404 as any other path,
and a valid one shows only the shop's company's and its own names, and its current answer.
"""

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from countertalk import consent
from countertalk.db import DbSession
from countertalk.models import ConsentDecision, ConsentLanguage

router = APIRouter(prefix="/api/consent", tags=["consent"])


class ConsentPageOut(BaseModel):
    company: str
    shop: str
    # The shop's current answer; None until it gives one.
    decision: ConsentDecision | None
    notice_version: int


class AnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ConsentDecision
    # The language the notice was read in.
    language: ConsentLanguage


def _out(page: consent.Page) -> ConsentPageOut:
    return ConsentPageOut(
        company=page.company,
        shop=page.shop,
        decision=page.decision,
        notice_version=page.notice_version,
    )


@router.get("/{token}")
def read_page(token: str, session: DbSession) -> ConsentPageOut:
    return _out(consent.page(session, token))


@router.post("/{token}")
def answer(token: str, body: AnswerIn, session: DbSession) -> ConsentPageOut:
    """I agree or No: it becomes the shop's current answer, and can be changed at any time."""
    return _out(consent.answer(session, token, body.decision, body.language))
