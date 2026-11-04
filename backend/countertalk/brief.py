"""The pre-visit brief (batch F5; BRIEF S2; the user, 4 Nov): before a visit, where the shop
stands. Code picks the facts from SQL, and Muse Spark writes only the words, when asked.

- **Available** once the shop has BRIEF_VISITS_MIN confirmed visits, by any rep.
- **Written only when asked** (Generate, Regenerate) by a rep, manager or admin, never by a job.
  Regenerate only once a newer confirmed visit exists: otherwise nothing changed. No daily cap
  (the user's call).
- **Saved:** each one a new row (models.Brief), so the latest is shown and older ones are kept.
  Reading one is free.
- **The facts,** sent and shown: the shop's confirmed visits (how many, the first and last);
  what it raises (each category, how often) and orders (each item, on how many visits); the
  last visit's problems (each with a shopkeeper's quote, in their own words) and order; its
  problems still open (fixed.shop_pending), and those fixed since its last visit
  (fixed.since_last_visit) with the line to tell them. Never a transcript, never a rep's name.
- **The words:** an overview of up to BRIEF_OVERVIEW_SENTENCES, and up to
  BRIEF_RAISING_SENTENCES worth raising this time, in English. A sentence with a number that
  isn't in the facts (batch 15b's check), or naming one of the org's users, is dropped, and
  counted by why. With none left, the brief is its facts.
- **One call** (LlmPurpose brief, logged with its cost), with no transaction open (§2.4). A
  failed call saves nothing. Two at once: the one that finishes second keeps the first's brief.
"""

import json
import logging
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, TypedDict

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from countertalk import entities, extraction, limits, llm_calls, rows
from countertalk.db import session_scope
from countertalk.errors import Conflict, NotFound, PermanentError
from countertalk.fixed import shop_pending, since_last_visit
from countertalk.llm import pricing
from countertalk.llm.base import Message, StructuredLlm
from countertalk.llm.client import not_configured
from countertalk.llm.schema import list_schema, object_schema, text_schema
from countertalk.models import (
    Brief,
    Evidence,
    Insight,
    LlmPurpose,
    OrderItem,
    Shop,
    User,
    Visit,
    VisitStatus,
    ist_date,
    utc_now,
)
from countertalk.narrative import numbers_in
from countertalk.playbooks import OrgPack
from countertalk.roles import SHOPKEEPER
from countertalk.users import CurrentUser

log = logging.getLogger(__name__)

NAME = "pre_visit_brief"
# A shop's brief's state.
UP_TO_DATE = "up_to_date"
OUT_OF_DATE = "out_of_date"
NOT_ENOUGH = "not_enough_visits"
NONE = "none"
# Why a sentence was dropped.
DROPPED = ("extra", "number", "name")
STATUS_NAMES = {"open": "Open", "in_progress": "In progress"}

PROMPT = """You write a short pre-visit brief for an FMCG company's sales rep, who is about to \
visit a small shop (a kirana) in India. You never see the visits: the user message is JSON with \
the facts code picked from the shop's past visits. Use only those facts.

Write, in plain, friendly English:
- overview: 2 to 4 sentences on what this shop raises, what it orders, and how things stand now.
- worth_raising: 1 or 2 sentences on what is worth raising with the shopkeeper this time: a \
problem still open to follow up, a fix to tell them about, or an order to suggest from what \
they usually buy.

Rules:
- Every number you write must be one in the facts. Don't count, add up or estimate. Write \
dates as the facts give them.
- Never name a rep or anyone at the company, and never guess a name.
- Don't invent problems, products, prices or promises. If the facts don't say it, leave it out.
- The shopkeeper's quotes are in their own words and language: never translate or change them \
inside quotation marks. You may say what they meant in English.
- Each sentence is one item of its list, with no bullet or number in front."""


class Raised(TypedDict):
    category: str
    problem: str
    # A shopkeeper's words, as said; None without one.
    quote: str | None


class Ordered(TypedDict):
    name: str
    quantity: float | None
    unit: str | None


class LastVisit(TypedDict):
    visited_at: str
    raised: list[Raised]
    ordered: list[Ordered]


class StillOpen(TypedDict):
    team: str
    status: str
    problem: str
    raised_at: str


class FixedSince(TypedDict):
    team: str
    problem: str
    tell_them: str
    resolved_at: str


class Content(TypedDict):
    """A brief as saved: what it shows, the AI's checked words, and the facts it was given."""

    overview: list[str]
    last_visit: LastVisit
    still_open: list[StillOpen]
    fixed_since: list[FixedSince]
    worth_raising: list[str]
    dropped: dict[str, int]
    facts: dict[str, Any]


@dataclass(frozen=True)
class Saved:
    id: str
    created_at: datetime
    # The shop's confirmed visits it covers: "after visit N".
    visits: int
    content: Content


@dataclass(frozen=True)
class Standing:
    """A shop, its confirmed visits now, and its latest brief."""

    shop_id: str
    name: str
    area: str
    visits: int
    brief: Saved | None

    @property
    def since(self) -> int:
        """Confirmed visits since its latest brief."""
        return max(0, self.visits - self.brief.visits) if self.brief is not None else 0

    @property
    def state(self) -> str:
        if self.brief is not None:
            return OUT_OF_DATE if self.since > 0 else UP_TO_DATE
        return NONE if self.visits >= limits.BRIEF_VISITS_MIN else NOT_ENOUGH

    @property
    def can_generate(self) -> bool:
        """Generate (none yet) or Regenerate (a newer confirmed visit)."""
        return self.state in (NONE, OUT_OF_DATE)


@dataclass(frozen=True)
class Picked:
    """What a brief is written from: the facts sent, what it shows, and what it covers."""

    facts: dict[str, Any]
    last_visit: LastVisit
    still_open: list[StillOpen]
    fixed_since: list[FixedSince]
    visit_id: str
    visits: int


# --- a shop's state ---------------------------------------------------------------------------


def _confirmed(org_id: str) -> list[Any]:
    return [Visit.org_id == org_id, Visit.status == VisitStatus.CONFIRMED]


def _counts(session: Session, org_id: str, shop_id: str | None) -> dict[str, int]:
    query = select(Visit.shop_id, func.count()).where(*_confirmed(org_id)).group_by(Visit.shop_id)
    if shop_id is not None:
        query = query.where(Visit.shop_id == shop_id)
    return {shop: count for shop, count in session.execute(query)}


def _latest(session: Session, org_id: str, shop_id: str | None) -> dict[str, Saved]:
    """Each shop's latest brief."""
    query = (
        select(Brief)
        .where(Brief.org_id == org_id)
        .order_by(Brief.shop_id, Brief.created_at.desc(), Brief.id.desc())
        .ext(distinct_on(Brief.shop_id))
    )
    if shop_id is not None:
        query = query.where(Brief.shop_id == shop_id)
    return {
        brief.shop_id: Saved(brief.id, brief.created_at, brief.visits, brief.content)  # type: ignore[arg-type]
        for brief in session.scalars(query)
    }


def missing_shop() -> NotFound:
    return NotFound("shop_not_found", "There's no such shop")


def of_shop(session: Session, org_id: str, shop_id: str) -> Standing:
    shop = rows.find_one(session, Shop, org_id, shop_id, missing_shop())
    visits = _counts(session, org_id, shop.id).get(shop.id, 0)
    return Standing(
        shop.id, shop.name, shop.area, visits, _latest(session, org_id, shop.id).get(shop.id)
    )


def every_shop(session: Session, org_id: str) -> list[Standing]:
    """Every shop of the org: those with a brief first, the newest brief first; then those that
    could have one; then those without enough visits yet; each by name."""
    counts = _counts(session, org_id, None)
    latest = _latest(session, org_id, None)
    shops = session.scalars(
        select(Shop).where(Shop.org_id == org_id).order_by(Shop.name, Shop.area)
    )
    found = [Standing(s.id, s.name, s.area, counts.get(s.id, 0), latest.get(s.id)) for s in shops]
    briefed = [each for each in found if each.brief is not None]
    briefed.sort(key=lambda each: (each.brief.created_at, each.brief.id), reverse=True)  # type: ignore[union-attr]
    rest = [each for each in found if each.brief is None]
    rest.sort(key=lambda each: each.state != NONE)  # stable: by name within each
    return briefed + rest


# --- the facts ---------------------------------------------------------------------------------


def _day(moment: datetime) -> str:
    found: date = ist_date(moment)
    return f"{found.day} {found:%b %Y}"


def pick(session: Session, pack: OrgPack, org_id: str, shop: Standing) -> Picked:
    """The shop's facts (the module docstring), at most BRIEF_ITEMS_MAX of each list."""
    most = limits.BRIEF_ITEMS_MAX
    categories = {key: found.name for key, found in pack.playbook.categories.items()}
    visits = session.execute(
        select(Visit.id, Visit.created_at)
        .where(*_confirmed(org_id), Visit.shop_id == shop.shop_id)
        .order_by(Visit.created_at, Visit.id)
    ).all()
    last_id, last_at = visits[-1]
    raised_counts = session.execute(
        select(Insight.category, func.count())
        .join(Visit, Visit.id == Insight.visit_id)
        .where(*_confirmed(org_id), Visit.shop_id == shop.shop_id, Insight.visible)
        .group_by(Insight.category)
        .order_by(func.count().desc(), Insight.category)
    ).all()
    orders = session.execute(
        select(OrderItem.visit_id, OrderItem.name, OrderItem.sku_id, OrderItem.quantity)
        .add_columns(OrderItem.unit)
        .join(Visit, Visit.id == OrderItem.visit_id)
        .where(*_confirmed(org_id), Visit.shop_id == shop.shop_id)
        .order_by(OrderItem.visit_id, OrderItem.seq)
    ).all()
    linked = entities.item_names(session, org_id, (order.sku_id for order in orders))
    named = [(order, linked.get(order.sku_id or "", order.name)) for order in orders]
    # Each item, on how many visits.
    ordered_on = Counter(name for _, name in {(o.visit_id, name) for o, name in named})
    last_raised = _raised(session, org_id, last_id, categories)[:most]
    last_ordered: list[Ordered] = [
        Ordered(
            name=name,
            quantity=float(order.quantity) if order.quantity is not None else None,
            unit=order.unit,
        )
        for order, name in named
        if order.visit_id == last_id
    ][:most]
    still_open = [
        StillOpen(
            team=p.team, status=p.status, problem=p.problem, raised_at=p.raised_at.isoformat()
        )
        for p in shop_pending(session, org_id, shop.shop_id)[:most]
    ]
    fixed = [
        FixedSince(
            team=f.team, problem=f.problem, tell_them=f.note, resolved_at=f.resolved_at.isoformat()
        )
        for f in since_last_visit(session, org_id, shop.shop_id)[:most]
    ]
    facts = {
        "today": _day(utc_now()),
        "shop": {"name": shop.name, "area": shop.area},
        "confirmed_visits": len(visits),
        "first_visit": _day(visits[0][1]),
        "last_visit": _day(last_at),
        "raised_on_all_visits": [
            {"category": categories.get(key, key), "times": count}
            for key, count in raised_counts[:most]
        ],
        "ordered_on_all_visits": [
            {"item": name, "visits": count} for name, count in ordered_on.most_common(most)
        ],
        "last_visit_raised": last_raised,
        "last_visit_ordered": last_ordered,
        "still_open": [
            {
                "team": p["team"],
                "status": STATUS_NAMES.get(p["status"], p["status"]),
                "problem": p["problem"],
                "raised_on": _day(datetime.fromisoformat(p["raised_at"])),
            }
            for p in still_open
        ],
        "fixed_since_last_visit": [
            {
                "team": f["team"],
                "problem": f["problem"],
                "tell_them": f["tell_them"],
                "resolved_on": _day(datetime.fromisoformat(f["resolved_at"])),
            }
            for f in fixed
        ],
    }
    shown = LastVisit(visited_at=last_at.isoformat(), raised=last_raised, ordered=last_ordered)
    return Picked(facts, shown, still_open, fixed, last_id, len(visits))


def _raised(
    session: Session, org_id: str, visit_id: str, categories: dict[str, str]
) -> list[Raised]:
    """The visit's shown insights, in order, each with its first shopkeeper's quote, as said."""
    quote = (
        select(Evidence.quote_final)
        .where(
            Evidence.insight_id == Insight.id,
            Evidence.speaker_role == SHOPKEEPER,
            Evidence.quote_final.is_not(None),
        )
        .order_by(Evidence.seq)
        .limit(1)
        .scalar_subquery()
    )
    found = session.execute(
        select(Insight.category, Insight.summary_en, quote)
        .where(Insight.org_id == org_id, Insight.visit_id == visit_id, Insight.visible)
        .order_by(Insight.seq)
    ).all()
    return [
        Raised(category=categories.get(key, key), problem=summary, quote=said)
        for key, summary, said in found
    ]


# --- the words ---------------------------------------------------------------------------------


def schema() -> dict[str, Any]:
    sentence = text_schema("One sentence, in English.")
    return object_schema(
        {"overview": list_schema(sentence), "worth_raising": list_schema(sentence)}
    )


def messages(facts: dict[str, Any]) -> list[Message]:
    return [
        Message("system", PROMPT),
        Message("user", json.dumps(facts, ensure_ascii=False, indent=1)),
    ]


def _numbers_of(value: Any) -> Iterator[float]:
    """Every number in the facts: each value, each said in a text, and each list's length."""
    if isinstance(value, str):
        yield from numbers_in(value)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _numbers_of(item)
    elif isinstance(value, list):
        yield float(len(value))
        for item in value:
            yield from _numbers_of(item)
    elif isinstance(value, int | float) and not isinstance(value, bool):
        yield float(value)


def _named(text: str, names: list[str]) -> bool:
    lowered = text.lower()
    return any(re.search(rf"(?<!\w){re.escape(name)}(?!\w)", lowered) for name in names)


def checked(
    answer: dict[str, Any], facts: dict[str, Any], names: list[str]
) -> tuple[list[str], list[str], dict[str, int]]:
    """The overview and worth raising as kept, and how many sentences were dropped, by why.
    `names` are the org's users' names and first names, lowercased; any the facts say
    themselves (a shop named after one) aren't counted."""
    allowed = set(_numbers_of(facts))
    said = json.dumps(facts, ensure_ascii=False).lower()
    names = [name for name in names if not _named(said, [name])]
    dropped = dict.fromkeys(DROPPED, 0)

    def keep(found: Any, room: int) -> list[str]:
        kept: list[str] = []
        for text in found if isinstance(found, list) else []:
            text = text.strip() if isinstance(text, str) else ""
            if not text:
                continue
            why = None
            if len(kept) >= room:
                why = "extra"
            elif any(n not in allowed for n in numbers_in(text)):
                why = "number"
            elif _named(text, names):
                why = "name"
            if why is None:
                kept.append(text)
            else:
                dropped[why] += 1
        return kept

    overview = keep(answer.get("overview"), limits.BRIEF_OVERVIEW_SENTENCES)
    raising = keep(answer.get("worth_raising"), limits.BRIEF_RAISING_SENTENCES)
    return overview, raising, dropped


def _names(session: Session, org_id: str) -> list[str]:
    """The org's users' names and first names (3 letters or more), lowercased."""
    found = set()
    for name in session.scalars(select(User.name).where(User.org_id == org_id)):
        name = name.strip().lower()
        if name:
            found.add(name)
            first = name.split()[0]
            if len(first) >= 3:
                found.add(first)
    return sorted(found)


# --- writing one -------------------------------------------------------------------------------


def _allowed(shop: Standing) -> None:
    """409 brief_not_enough_visits before BRIEF_VISITS_MIN; 409 brief_up_to_date with no newer
    confirmed visit since the latest."""
    if shop.state == NOT_ENOUGH:
        raise Conflict(
            "brief_not_enough_visits",
            f"A brief needs at least {limits.BRIEF_VISITS_MIN} confirmed visits to this shop",
        )
    if shop.state == UP_TO_DATE:
        raise Conflict(
            "brief_up_to_date", "The brief is up to date: there's no confirmed visit since"
        )


def generate(llm: StructuredLlm | None, user: CurrentUser, shop_id: str) -> Standing:
    """A new brief for the shop (the module docstring): 404 for a shop that isn't the org's,
    then 503 without a key, then 409 when it isn't allowed now. The shop's state afterwards."""
    with session_scope(read_only=True) as session:
        shop = of_shop(session, user.org_id, shop_id)
        if llm is None:
            raise not_configured()
        _allowed(shop)
        pack = extraction.pack_for(user.org_id, session=session)
        picked = pick(session, pack, user.org_id, shop)
        names = _names(session, user.org_id)
    outcome = llm.complete(messages(picked.facts), NAME, schema())
    brief_id = None
    try:
        unreadable = PermanentError("brief_answer_invalid", "Muse Spark's answer didn't fit")
        answer = llm_calls.answer_of(outcome, unreadable)
        overview, raising, dropped = checked(answer, picked.facts, names)
        content = Content(
            overview=overview,
            last_visit=picked.last_visit,
            still_open=picked.still_open,
            fixed_since=picked.fixed_since,
            worth_raising=raising,
            dropped=dropped,
            facts=picked.facts,
        )
        brief_id = _save(user, shop.shop_id, picked, content, outcome.calls[-1].model)
    finally:  # paid for, whatever happened: counted, with its brief if one was saved
        llm_calls.record(None, LlmPurpose.BRIEF, llm.provider, outcome, brief_id=brief_id)
    usd = sum(map(pricing.call_cost, outcome.calls))
    log.info(
        "brief written shop=%s brief=%s kept=%d dropped=%s usd=%.4f",
        shop.shop_id,
        brief_id,
        len(overview) + len(raising),
        dropped,
        usd,
    )
    with session_scope(read_only=True) as session:
        return of_shop(session, user.org_id, shop.shop_id)


def _save(
    user: CurrentUser, shop_id: str, picked: Picked, content: Content, model: str
) -> str | None:
    """The new brief, under the shop's row lock: its ID. None when another was written
    meanwhile covering as much: that one is kept."""
    with session_scope() as session:
        rows.find_one(session, Shop, user.org_id, shop_id, missing_shop(), for_update=True)
        latest = _latest(session, user.org_id, shop_id).get(shop_id)
        if latest is not None and latest.visits >= picked.visits:
            return None
        row = Brief(
            org_id=user.org_id,
            shop_id=shop_id,
            user_id=user.id,
            visit_id=picked.visit_id,
            visits=picked.visits,
            content=content,
            model=model,
        )
        session.add(row)
        session.flush()
        return row.id
