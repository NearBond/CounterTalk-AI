"""Ask a question across visits (batch 17d): a manager types a question, Muse Spark turns it into
the inbox's filter, and code checks it and runs it. The AI suggests, code decides: the model
never writes SQL and never sees a visit, and the question is only ever text in the prompt,
never part of a query.

1. **The vocabulary**, from the org: its playbook's categories, its catalog's competitors and
   own products (keys, with their names), and its shops' areas. The strict JSON schema's enums
   are these keys; the prompt lists them with their names, and today's IST date.
2. **One call** (LlmPurpose ask, logged like the others, its cost too), with no transaction
   open (§2.4).
3. **The answer checked again:** every key must be the org's. One that isn't, or
   `understood: false`, is an answer saying what wasn't understood, and runs no query.
4. **The filter run** at the finding level (inbox.findings, with 17c's filters and a brand
   taking in its products): newest first, at most ASK_FINDINGS_MAX. A team member's are their
   own team's issues' only, as their inbox is.

A user asks at most ASK_DAILY_MAX questions an IST day (the public deploy's cost guard): the
asks row is written under the user's row lock before the call, so questions at once can't pass
the limit together, and one whose call failed still counts. Without MODEL_API_KEY nothing is
sent or counted: 503, as other AI steps say.
"""

import json
import logging
import math
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from countertalk import auth, extraction, inbox, limits, llm_calls, rows
from countertalk.db import session_scope
from countertalk.entities import item_name
from countertalk.errors import PermanentError, TooMany
from countertalk.llm import pricing
from countertalk.llm.base import Message, Outcome, StructuredLlm
from countertalk.llm.client import not_configured
from countertalk.llm.schema import enum_schema, nullable_schema, object_schema, text_schema
from countertalk.models import (
    ACTIVE_ISSUE,
    Ask,
    CatalogItem,
    CatalogKind,
    IssueStatus,
    LlmPurpose,
    Role,
    Shop,
    Team,
    User,
    ist_date,
    utc_now,
)
from countertalk.playbooks import OrgPack, playbook_text
from countertalk.summaries import at_midnight
from countertalk.users import CurrentUser

log = logging.getLogger(__name__)

LAST_30_DAYS = "last_30_days"
# The answer's status, as the inbox's statuses.
STATUSES: dict[str, tuple[str, ...]] = {
    "open": tuple(status.value for status in ACTIVE_ISSUE),
    "resolved": (IssueStatus.RESOLVED.value,),
    "any": (*(status.value for status in ACTIVE_ISSUE), IssueStatus.RESOLVED.value),
}
STATUS_NAMES = {"open": "Open and in progress", "resolved": "Resolved", "any": "Any status"}
NOT_UNDERSTOOD = "The question couldn't be turned into a filter"
REASON_MAX_CHARS = 200


@dataclass(frozen=True)
class Vocabulary:
    """What the question may name, by key, with each one's name."""

    categories: dict[str, str]
    competitors: dict[str, str]
    products: dict[str, str]
    areas: dict[str, str]
    # Each catalog key's item ID.
    items: dict[str, str]


@dataclass(frozen=True)
class Answer:
    understood: bool
    # Why not, when it wasn't.
    reason: str | None
    # What was understood, as (what, its name): the answer's chips.
    chips: list[tuple[str, str]]
    findings: list[inbox.Finding]


def vocabulary(session: Session, pack: OrgPack, org_id: str) -> Vocabulary:
    found = session.execute(
        select(CatalogItem.key, CatalogItem.id, CatalogItem.kind)
        .add_columns(CatalogItem.brand, CatalogItem.product)
        .where(CatalogItem.org_id == org_id)
        .order_by(CatalogItem.key)
    ).all()
    named = {
        kind: {row.key: item_name(row.brand, row.product) for row in found if row.kind == kind}
        for kind in CatalogKind
    }
    areas = session.scalars(
        select(Shop.area).where(Shop.org_id == org_id).distinct().order_by(Shop.area)
    )
    return Vocabulary(
        categories={key: found.name for key, found in pack.playbook.categories.items()},
        competitors=named[CatalogKind.COMPETITOR],
        products=named[CatalogKind.OWN],
        areas={area: area for area in areas},
        items={row.key: row.id for row in found},
    )


def periods(today: date) -> dict[str, tuple[str, date, date]]:
    """Each period the answer may name: (its name, first day, last day), in IST."""
    found = {p.key: (p.label, p.first_day, p.last_day) for p in inbox.periods(today)}
    first = today - timedelta(days=limits.ASK_LAST_DAYS - 1)
    found[LAST_30_DAYS] = (f"Last {limits.ASK_LAST_DAYS} days", first, today)
    return found


def schema(vocabulary: Vocabulary, today: date) -> dict[str, Any]:
    """The answer's strict schema: every key one of the org's, or null."""
    return object_schema(
        {
            "category": nullable_schema(enum_schema(vocabulary.categories)),
            "competitor": nullable_schema(enum_schema(vocabulary.competitors)),
            "product": nullable_schema(enum_schema(vocabulary.products)),
            "area": nullable_schema(enum_schema(vocabulary.areas)),
            "period": nullable_schema(enum_schema(periods(today))),
            "status": enum_schema(STATUSES),
            "understood": {"type": "boolean"},
            "reason": nullable_schema(text_schema("Why it wasn't understood, in a few words.")),
        }
    )


def messages(pack: OrgPack, vocabulary: Vocabulary, question: str, today: date) -> list[Message]:
    """The prompt, then the choices and the question as JSON: the question is only ever text."""

    def choices(names: dict[str, str]) -> list[dict[str, str]]:
        return [{"key": key, "name": name} for key, name in names.items()]

    payload = {
        "today": f"{today.isoformat()} ({today:%A})",
        "categories": choices(vocabulary.categories),
        "competitors": choices(vocabulary.competitors),
        "products": choices(vocabulary.products),
        "areas": list(vocabulary.areas),
        "periods": list(periods(today)),
        "statuses": list(STATUSES),
        "question": question,
    }
    return [
        Message("system", playbook_text(pack, "ask_prompt")),
        Message("user", json.dumps(payload, ensure_ascii=False, indent=1)),
    ]


def understood(
    answer: dict[str, Any], vocabulary: Vocabulary, today: date
) -> tuple[inbox.Filters, list[tuple[str, str]]] | str:
    """The answer checked again: its filter and chips, or why it wasn't understood. Every key
    must be the org's, whatever the schema promised."""
    if answer.get("understood") is not True:
        reason = answer.get("reason")
        if isinstance(reason, str) and reason.strip():
            return reason.strip()[:REASON_MAX_CHARS]
        return NOT_UNDERSTOOD
    named = {
        "category": ("Category", vocabulary.categories),
        "competitor": ("Competitor", vocabulary.competitors),
        "product": ("Product", vocabulary.products),
        "area": ("Area", vocabulary.areas),
        "period": ("Period", {key: found[0] for key, found in periods(today).items()}),
    }
    picked: dict[str, str] = {}
    chips = []
    for field, (what, names) in named.items():
        value = answer.get(field)
        if value is None:
            continue
        if not isinstance(value, str) or value not in names:
            return f"It named a {field} that isn't one of the org's"
        picked[field] = value
        chips.append((what, names[value]))
    status = answer.get("status")
    if not isinstance(status, str) or status not in STATUSES:
        return "It named a status that isn't open, resolved or any"
    chips.append(("Status", STATUS_NAMES[status]))
    first_day = last_day = None
    if "period" in picked:
        _, first_day, last_day = periods(today)[picked["period"]]
    filters = inbox.Filters(
        statuses=STATUSES[status],
        category=picked.get("category"),
        area=picked.get("area"),
        competitor_id=vocabulary.items.get(picked.get("competitor", "")),
        sku_id=vocabulary.items.get(picked.get("product", "")),
        first_day=first_day,
        last_day=last_day,
    )
    return filters, chips


def ask(llm: StructuredLlm | None, user: CurrentUser, question: str) -> Answer:
    """The question answered (the module docstring). 503 without a key, 429 past the limit."""
    if llm is None:
        raise not_configured()
    with session_scope(read_only=True) as session:
        pack = extraction.pack_for(user.org_id, session=session)
        found = vocabulary(session, pack, user.org_id)
    today = ist_date(utc_now())
    ask_id = _counted(user)
    asked = messages(pack, found, question, today)
    outcome = llm.complete(asked, "inbox_filter", schema(found, today))
    llm_calls.record(None, LlmPurpose.ASK, llm.provider, outcome, ask_id=ask_id)
    checked = _checked(outcome, found, today)
    usd = sum(map(pricing.call_cost, outcome.calls))
    if isinstance(checked, str):
        log.info("ask not understood ask=%s usd=%.4f", ask_id, usd)
        return Answer(False, checked, [], [])
    filters, chips = checked
    team_id = user.team_id if user.role == Role.TEAM_MEMBER else None
    with session_scope(read_only=True) as session:
        if team_id is not None:
            chips = [("Team", session.get_one(Team, team_id).name), *chips]
        shown = inbox.findings(session, user.org_id, filters, team_id, limits.ASK_FINDINGS_MAX)
    if team_id is not None:  # only their team's route, as review.team_part shows a visit
        shown = [_their_part(finding, team_id) for finding in shown]
    log.info("ask answered ask=%s findings=%d usd=%.4f", ask_id, len(shown), usd)
    return Answer(True, None, chips, shown)


def _their_part(finding: inbox.Finding, team_id: str) -> inbox.Finding:
    routes = [route for route in finding.insight.routes if route.team_id == team_id]
    return replace(finding, insight=replace(finding.insight, routes=routes))


def _checked(
    outcome: Outcome, vocabulary: Vocabulary, today: date
) -> tuple[inbox.Filters, list[tuple[str, str]]] | str:
    """understood() of the answer; an answer that didn't fit even after its repair isn't
    understood either (asking again pays again for the same). Any other failure is raised:
    503 for one a retry may fix."""
    unreadable = PermanentError("ask_answer_invalid", "Muse Spark's answer didn't fit its schema")
    try:
        answer = llm_calls.answer_of(outcome, unreadable)
    except PermanentError as error:
        if error is unreadable:
            return NOT_UNDERSTOOD
        raise
    return understood(answer, vocabulary, today)


def _counted(user: CurrentUser) -> str:
    """The question's asks row, written under the user's row lock: 429 once they've asked
    ASK_DAILY_MAX today (IST), until tomorrow."""
    now = utc_now()
    today = ist_date(now)
    with session_scope() as session:
        rows.find_one(session, User, user.org_id, user.id, auth.missing_user(), for_update=True)
        asked = session.scalar(
            select(func.count())
            .select_from(Ask)
            .where(Ask.user_id == user.id, Ask.created_at >= at_midnight(today))
        )
        if (asked or 0) >= limits.ASK_DAILY_MAX:
            tomorrow = at_midnight(today + timedelta(days=1))
            raise TooMany(
                "ask_limit",
                f"That's {limits.ASK_DAILY_MAX} questions today, the most a day. Ask again"
                " tomorrow.",
                retry_after_s=max(1, math.ceil((tomorrow - now).total_seconds())),
            )
        row = Ask(org_id=user.org_id, user_id=user.id, created_at=now)
        session.add(row)
        session.flush()
        return row.id
