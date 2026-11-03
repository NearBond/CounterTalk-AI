"""Shops' consent to recording, given before any visit (batch 17; the user, 3 Nov).

- **By link:** a rep, manager or admin shares the shop's consent link (on WhatsApp, from their
  own phone). The shopkeeper opens it, reads the notice and taps I agree or No, and can change
  it at any time. Every link a shop is sent stays valid; only each token's hash is kept.
- **On paper:** the shopkeeper signs the consent form that goes with the invoice, and someone of
  the org marks it in the app: always a yes, saved with who marked it.

The notice the page shows is consent/v1/notice.yaml, in Hindi, English and Marathi, read
strictly; each answer records its version and the language it was read in.

A shop's latest answer (by seq) is its current one, and every answer is kept (the table is
append-only). A conversation needs a current yes: visits checks it under the shop's row lock,
which an answer takes too, so the two never cross. do_not_record still blocks as before.
"""

import logging
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from http import HTTPStatus
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from countertalk import auth
from countertalk.config import BACKEND_DIR
from countertalk.errors import Conflict, NotFound
from countertalk.models import (
    CONSENT_LANGUAGES,
    Consent,
    ConsentDecision,
    ConsentLink,
    ConsentMethod,
    Org,
    Shop,
    parse_id,
)
from countertalk.playbooks import PackError, first_problem, load_org_pack, read_yaml

log = logging.getLogger(__name__)

# consent/v1's notice: the version every answer records.
NOTICE_VERSION = 1
NOTICE_PATH = BACKEND_DIR / "consent" / f"v{NOTICE_VERSION}" / "notice.yaml"
# A token as new_link makes it (secrets.token_urlsafe(32)): anything else is no token at all.
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


Text = Annotated[str, StringConstraints(min_length=1)]


class NoticeText(BaseModel):
    """The notice in one language: what the consent page says, and the message a shared link
    goes with."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    name: Text
    title: Text
    what: Text
    why: Text
    benefits_title: Text
    benefits: list[Text]
    real_time: Text
    retention: Text
    no_change: Text
    change_mind: Text
    agree: Text
    refuse: Text
    answered_yes: Text
    answered_no: Text
    not_answered: Text
    share_message: Text


class NoticeFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: int
    languages: dict[str, NoticeText]


@cache
def notice() -> dict[str, NoticeText]:
    """The notice by language, in the file's order: every language a consent may be in, for
    this version, and no other. A file that doesn't check out is a PackError naming the field."""
    data = read_yaml(NOTICE_PATH)
    try:
        found = NoticeFile.model_validate(data)
    except ValidationError as error:
        raise PackError(NOTICE_PATH, *first_problem(error)) from None
    if found.version != NOTICE_VERSION:
        raise PackError(NOTICE_PATH, "version", f"is {found.version}, not {NOTICE_VERSION}")
    if set(found.languages) != set(CONSENT_LANGUAGES):
        shown, wanted = ", ".join(found.languages), ", ".join(CONSENT_LANGUAGES)
        raise PackError(NOTICE_PATH, "languages", f"are {shown}, not {wanted}")
    return found.languages


def share_message() -> str:
    """What a shared link goes with: the notice's message in each language, a line each."""
    return "\n".join(text.share_message for text in notice().values())


def no_such_page() -> NotFound:
    """The very 404 of a path the app hasn't got: a wrong token says nothing about any shop."""
    return NotFound("not_found", HTTPStatus.NOT_FOUND.phrase)


def current(session: Session, org_id: str, shop_ids: Iterable[str]) -> dict[str, str]:
    """Each of these shops' current decision, for those that have answered."""
    ids = list(shop_ids)
    if not ids:
        return {}
    latest = (
        select(func.max(Consent.seq))
        .where(Consent.org_id == org_id, Consent.shop_id.in_(ids))
        .group_by(Consent.shop_id)
    )
    query = select(Consent.shop_id, Consent.decision).where(Consent.seq.in_(latest))
    return {shop_id: decision for shop_id, decision in session.execute(query)}


def _latest(session: Session, shop: Shop) -> str | None:
    return session.scalars(
        select(Consent.decision)
        .where(Consent.org_id == shop.org_id, Consent.shop_id == shop.id)
        .order_by(Consent.seq.desc())
        .limit(1)
    ).first()


def agreed(session: Session, shop: Shop) -> bool:
    """Whether the shop's current answer is yes: one row, as every segment asks. Ask under
    the shop's row lock (visits does)."""
    return _latest(session, shop) == ConsentDecision.YES


def _shop(session: Session, org_id: str, shop_id: str, *, lock: bool) -> Shop:
    """The org's shop; with `lock`, FOR UPDATE, so a visit being started for it waits for the
    answer (R6)."""
    found = parse_id(shop_id)
    shop = None
    if found is not None:
        query = select(Shop).where(Shop.id == found, Shop.org_id == org_id)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        shop = session.scalars(query).one_or_none()
    if shop is None:
        raise NotFound("shop_not_found", "There's no such shop")
    return shop


def _record(
    session: Session, shop: Shop, decision: str, method: str, language: str, by: str | None
) -> Consent:
    answer = Consent(
        org_id=shop.org_id,
        shop_id=shop.id,
        decision=decision,
        method=method,
        notice_version=NOTICE_VERSION,
        language=language,
        recorded_by=by,
    )
    session.add(answer)
    session.flush()
    log.info("consent recorded shop=%s decision=%s method=%s", shop.id, decision, method)
    return answer


def mark_paper(session: Session, org_id: str, shop_id: str, user_id: str, language: str) -> str:
    """The shopkeeper signed the paper form: a yes, marked by this user. The shop's answer.
    A no the shop gave on its page stands: only the shop changes it, on its link."""
    shop = _shop(session, org_id, shop_id, lock=True)
    if _latest(session, shop) == ConsentDecision.NO:
        raise Conflict(
            "consent_refused",
            "This shop said no on its consent page. It can change that there: share its link again",
        )
    return _record(
        session, shop, ConsentDecision.YES, ConsentMethod.PAPER, language, user_id
    ).decision


def mark_unanswered(session: Session, org_id: str, user_id: str, language: str) -> int:
    """A paper yes, marked by this user, for each of the org's shops that hasn't answered: for
    seeds and dev data (the user, 3 Nov: every shop in the data has consented). An answer,
    yes or no, is never changed. How many it marked."""
    shops = session.scalars(
        select(Shop).where(Shop.org_id == org_id).order_by(Shop.id).with_for_update()
    ).all()
    answered = current(session, org_id, [shop.id for shop in shops])
    marked = [shop for shop in shops if shop.id not in answered]
    for shop in marked:
        _record(session, shop, ConsentDecision.YES, ConsentMethod.PAPER, language, user_id)
    return len(marked)


def new_link(session: Session, org_id: str, shop_id: str, user_id: str) -> str:
    """A new link token for the shop's consent page, to share. Only its hash is kept, and the
    shop's other links stay valid."""
    shop = _shop(session, org_id, shop_id, lock=False)
    token = secrets.token_urlsafe(32)
    session.add(
        ConsentLink(
            org_id=shop.org_id,
            shop_id=shop.id,
            token_hash=auth.token_hash(token),
            created_by=user_id,
        )
    )
    session.flush()
    log.info("consent link made shop=%s", shop.id)
    return token


def _shop_of(session: Session, token: str, *, for_update: bool) -> Shop:
    if not isinstance(token, str) or TOKEN.fullmatch(token) is None:
        raise no_such_page()
    link = session.scalars(
        select(ConsentLink).where(ConsentLink.token_hash == auth.token_hash(token))
    ).one_or_none()
    if link is None:
        raise no_such_page()
    return _shop(session, link.org_id, link.shop_id, lock=for_update)


@cache
def _pack_name(pack_id: str, version: int) -> str | None:
    """A pack's display name, read once per process: the page is public, so a reload
    never parses a pack again. None for one that won't load (logged; the page shows the
    org's own name)."""
    try:
        return load_org_pack(pack_id, version).file.display_name
    except PackError:
        log.warning("consent page without its pack pack=%s version=%d", pack_id, version)
        return None


@dataclass(frozen=True)
class Page:
    """All the consent page shows of the shop: its company's and its own names, and its answer."""

    company: str
    shop: str
    decision: str | None
    notice_version: int


def _page(session: Session, shop: Shop) -> Page:
    org = session.get_one(Org, shop.org_id)
    company = org.name
    if org.org_pack_id is not None and org.org_pack_version is not None:
        company = _pack_name(org.org_pack_id, org.org_pack_version) or org.name
    decision = _latest(session, shop)
    return Page(company, shop.name, decision, NOTICE_VERSION)


def page(session: Session, token: str) -> Page:
    return _page(session, _shop_of(session, token, for_update=False))


def answer(session: Session, token: str, decision: str, language: str) -> Page:
    """The shopkeeper's answer on the page: it becomes the shop's current one."""
    shop = _shop_of(session, token, for_update=True)
    _record(session, shop, decision, ConsentMethod.LINK, language, None)
    return _page(session, shop)
