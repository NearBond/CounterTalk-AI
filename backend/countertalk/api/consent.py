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


class NoticeTextOut(BaseModel):
    name: str
    title: str
    what: str
    why: str
    benefits_title: str
    benefits: list[str]
    real_time: str
    retention: str
    no_change: str
    change_mind: str
    agree: str
    refuse: str
    answered_yes: str
    answered_no: str
    not_answered: str


class ConsentPageOut(BaseModel):
    company: str
    shop: str
    # The shop's current answer; None until it gives one.
    decision: ConsentDecision | None
    notice_version: int
    # The notice in each language it's in, in the file's order: the page shows the one picked.
    notice: dict[ConsentLanguage, NoticeTextOut]


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
        notice={
            language: NoticeTextOut.model_validate(text.model_dump())
            for language, text in consent.notice().items()
        },
    )


@router.get("/{token}")
def read_page(token: str, session: DbSession) -> ConsentPageOut:
    return _out(consent.page(session, token))


@router.post("/{token}")
def answer(token: str, body: AnswerIn, session: DbSession) -> ConsentPageOut:
    """I agree or No: it becomes the shop's current answer, and can be changed at any time."""
    return _out(consent.answer(session, token, body.decision, body.language))
