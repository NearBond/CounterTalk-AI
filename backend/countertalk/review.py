"""The rep's review (batch 14; BRIEF §6): edits to a visit's insights, a finding the AI missed,
notes, and the confirm.

Each runs under the visit's lock and only while it's in `review` (§2.3): a confirmed visit gives
409 visit_confirmed, one still being processed (or failed) 409 visit_not_in_review. Each change
is an Edit row in the same transaction. A category or severity change re-routes through
routing.py, the only writer of routes. An unverified insight can be deleted, never edited.
Confirm makes the verified insights visible and locks the visit; confirming again changes
nothing (§2.5).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from countertalk import limits, roles, routing, verify, visits
from countertalk.errors import Conflict, Invalid, NotFound
from countertalk.models import (
    CREATED,
    DELETED,
    CatalogItem,
    Edit,
    EditObject,
    Evidence,
    EvidenceMatch,
    Insight,
    InsightRoute,
    Report,
    Task,
    Team,
    Turn,
    Visit,
    VisitNote,
    VisitStatus,
    new_id,
    parse_id,
)
from countertalk.playbooks import OrgPack

log = logging.getLogger(__name__)


def locked_in_review(session: Session, org_id: str, visit_id: str) -> Visit:
    visit = visits.lock_visit(session, org_id, visit_id)
    _require_review(visit)
    return visit


def _require_review(visit: Visit) -> None:
    """The one place a reviewable status is decided, and its two 409s."""
    if visit.status == VisitStatus.CONFIRMED:
        raise Conflict("visit_confirmed", "The visit is confirmed, so it can't be changed")
    if visit.status != VisitStatus.REVIEW:
        raise Conflict("visit_not_in_review", "The visit isn't ready for review")


def next_seq(session: Session, visit_id: str) -> int:
    """The seq after the visit's last insight's, or 0: for a rep's and an extraction's (R1)."""
    last = session.scalar(select(func.max(Insight.seq)).where(Insight.visit_id == visit_id))
    return last + 1 if last is not None else 0


def _locked_insight(session: Session, org_id: str, insight_id: str) -> tuple[Visit, Insight]:
    """The insight, read fresh once its visit is locked (R6). Another org's is 404."""
    found = parse_id(insight_id)
    where = (Insight.id == found, Insight.org_id == org_id)
    visit_id = session.scalar(select(Insight.visit_id).where(*where)) if found else None
    if visit_id is None:
        raise NotFound("insight_not_found", "There's no such insight")
    visit = locked_in_review(session, org_id, visit_id)
    query = select(Insight).where(*where).execution_options(populate_existing=True)
    insight = session.scalars(query).one_or_none()
    if insight is None:  # deleted while the lock was waited for
        raise NotFound("insight_not_found", "There's no such insight")
    return visit, insight


def _one_of(value: str, allowed: Any, what: str) -> str:
    if value not in allowed:
        raise Invalid(f"invalid_{what}", f"That isn't one of the playbook's {what} keys")
    return value


def _text(value: str) -> str:
    text = value.strip()
    if not text:
        raise Invalid("summary_empty", "The summary can't be empty")
    if len(text) > limits.SUMMARY_MAX_CHARS:
        raise Invalid(
            "summary_too_long", f"A summary is at most {limits.SUMMARY_MAX_CHARS} characters"
        )
    return text


def _edit_row(
    session: Session,
    visit: Visit,
    actor: str,
    what: tuple[EditObject, str],
    field: str,
    before: Any,
    after: Any,
) -> None:
    """The one way an Edit row is written: what changed is (object, its ID)."""
    session.add(
        Edit(
            org_id=visit.org_id,
            visit_id=visit.id,
            actor_id=actor,
            object=what[0],
            object_id=what[1],
            field=field,
            before=before,
            after=after,
        )
    )


def _route(session: Session, pack: OrgPack, insight: Insight, actor: str | None) -> None:
    """The rule's routes for the insight's category and severity, and its AI team: given to a
    new insight, or replacing an edited one's (a `reroute` by the actor)."""
    teams = routing.team_ids(session, insight.org_id)
    ai = routing.ai_team_key(session, insight, teams)
    table = routing.table_of(pack)
    decision = routing.decide(insight.category, insight.severity, ai, table, teams)
    if actor is None:
        routing.route_new(session, insight, decision, teams)
    else:
        routing.reroute(session, insight, decision, teams, actor)


def edit_insight(
    session: Session,
    pack_for: Callable[[], OrgPack],
    org_id: str,
    actor: str,
    insight_id: str,
    *,
    summary_en: str | None = None,
    category: str | None = None,
    severity: str | None = None,
) -> Insight:
    """A row per field changed; a category or severity change re-routes it. The org's pack is
    loaded only for those (pack_for), after the visit is found."""
    visit, insight = _locked_insight(session, org_id, insight_id)
    if not insight.verified:
        raise Conflict(
            "insight_unverified",
            "It couldn't be found in the recording, so it can't be edited: delete it, or add"
            " it from the transcript",
        )
    wanted: dict[str, str] = {}
    if summary_en is not None:
        wanted["summary_en"] = _text(summary_en)
    pack = pack_for() if category is not None or severity is not None else None
    if pack is not None and category is not None:
        wanted["category"] = _one_of(category, pack.playbook.categories, "category")
    if pack is not None and severity is not None:
        wanted["severity"] = _one_of(severity, pack.playbook.severities, "severity")
    changed = {field: value for field, value in wanted.items() if getattr(insight, field) != value}
    for field, value in changed.items():
        before = getattr(insight, field)
        _edit_row(session, visit, actor, (EditObject.INSIGHT, insight.id), field, before, value)
        setattr(insight, field, value)
    if pack is not None and {"category", "severity"} & changed.keys():
        _route(session, pack, insight, actor)
    log.info("insight edited visit=%s insight=%s fields=%d", visit.id, insight.id, len(changed))
    return insight


def delete_insight(session: Session, org_id: str, actor: str, insight_id: str) -> None:
    """Gone, with its quotes and routes; its Edit row keeps what it was, and its routing
    history stays (its insight_id has no foreign key, by design)."""
    visit, insight = _locked_insight(session, org_id, insight_id)
    was = {
        "kind": insight.kind,
        "category": insight.category,
        "severity": insight.severity,
        "summary_en": insight.summary_en,
        "verified": insight.verified,
        "added_by_rep": insight.added_by_rep,
    }
    _edit_row(session, visit, actor, (EditObject.INSIGHT, insight.id), DELETED, was, None)
    session.delete(insight)
    log.info("insight deleted visit=%s insight=%s", visit.id, insight.id)


def add_insight(
    session: Session,
    pack: OrgPack,
    org_id: str,
    actor: str,
    visit_id: str,
    *,
    turn_id: int,
    kind: str,
    category: str,
    severity: str,
    summary_en: str,
) -> Insight:
    """A finding the AI missed, quoting the whole of a turn of the visit's current transcript:
    exact, playable, and routed by the table (it has no AI team)."""
    visit = locked_in_review(session, org_id, visit_id)
    found = {
        "kind": _one_of(kind, pack.playbook.kinds, "kind"),
        "category": _one_of(category, pack.playbook.categories, "category"),
        "severity": _one_of(severity, pack.playbook.severities, "severity"),
        "summary_en": _text(summary_en),
    }
    turn = session.get(Turn, (visit.transcript_id, turn_id)) if visit.transcript_id else None
    if turn is None:
        raise NotFound("turn_not_found", "That line isn't in this visit's transcript")
    insight = Insight(
        id=new_id(),
        org_id=org_id,
        visit_id=visit.id,
        seq=next_seq(session, visit.id),
        extraction_id=None,
        sku_mentions=[],
        competitor_mentions=[],
        added_by_rep=True,
        verified=True,
        **found,
    )
    session.add(insight)
    session.flush()
    role = roles.saved_roles(session, turn.transcript_id).get(turn.speaker_label, (None,))[0]
    session.add(
        Evidence(
            insight_id=insight.id,
            visit_id=visit.id,
            seq=0,
            transcript_id=turn.transcript_id,
            turn_id=turn.turn_id,
            speaker_role=role,
            quote_llm=turn.text,
            quote_final=turn.text,
            match=EvidenceMatch.EXACT,
            char_start=0,
            char_end=len(turn.text),
            start_ms=verify.playback_start(turn.start_ms, 0),
        )
    )
    created = {"turn_id": turn_id, **found}
    _edit_row(session, visit, actor, (EditObject.INSIGHT, insight.id), CREATED, None, created)
    _route(session, pack, insight, None)
    log.info("insight added visit=%s insight=%s turn=%d", visit.id, insight.id, turn_id)
    return insight


def add_note(session: Session, org_id: str, author: str, visit_id: str, text: str) -> VisitNote:
    """For the visit's record only: never routed, never shown to teams."""
    visit = locked_in_review(session, org_id, visit_id)
    body = text.strip()
    if not body:
        raise Invalid("note_empty", "The note is empty")
    if len(body) > limits.NOTE_MAX_CHARS:
        raise Invalid("note_too_long", f"A note is at most {limits.NOTE_MAX_CHARS} characters")
    note = VisitNote(org_id=org_id, visit_id=visit.id, author_id=author, text=body)
    session.add(note)
    session.flush()
    log.info("note added visit=%s note=%s", visit.id, note.id)
    return note


def confirm(session: Session, org_id: str, actor: str, visit_id: str) -> Visit:
    """Review to confirmed: the verified insights become visible, and the visit is locked
    against edits. Confirming again returns it as it is (§2.5)."""
    visit = visits.lock_visit(session, org_id, visit_id)
    if visit.status == VisitStatus.CONFIRMED:
        return visit
    _require_review(visit)
    visits.set_status(visit, VisitStatus.CONFIRMED)
    session.execute(
        update(Insight).where(Insight.visit_id == visit.id).values(visible=Insight.verified)
    )
    was, now = VisitStatus.REVIEW.value, VisitStatus.CONFIRMED.value
    _edit_row(session, visit, actor, (EditObject.VISIT, visit.id), "status", was, now)
    log.info("visit confirmed visit=%s", visit.id)
    return visit


# --- reading ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class QuoteView:
    seq: int
    turn_id: int | None
    speaker_role: str | None
    # The transcript's own words where it was found; the model's where it wasn't.
    text: str
    match: str | None
    start_ms: int | None


@dataclass(frozen=True)
class RouteView:
    slot: int
    team_key: str
    team_name: str
    routed_by: str


@dataclass(frozen=True)
class InsightView:
    insight: Insight
    sku: str | None
    competitor: str | None
    ai_team: str | None
    quotes: list[QuoteView]
    routes: list[RouteView]


@dataclass(frozen=True)
class Choice:
    key: str
    label: str


@dataclass(frozen=True)
class ReviewView:
    visit: Visit
    report: Report | None
    tasks: list[Task]
    insights: list[InsightView]
    notes: list[VisitNote]
    kinds: list[Choice]
    categories: list[Choice]
    severities: list[Choice]


def _label(key: str) -> str:
    return key.replace("_", " ").capitalize()


def review_of(
    session: Session, pack_for: Callable[[], OrgPack], org_id: str, visit_id: str
) -> ReviewView:
    """What the review page shows: the report, tasks, insights with their quotes and teams,
    notes, and the playbook's keys for its pickers. Another org's visit is 404, before the pack
    is loaded. Read in one snapshot (a read-only session's)."""
    visit = visits.get_visit(session, org_id, visit_id)
    pack = pack_for()
    report = session.get(Report, visit.id)
    tasks = session.scalars(select(Task).where(Task.visit_id == visit.id).order_by(Task.seq))
    query = select(Insight).where(Insight.visit_id == visit.id).order_by(Insight.seq)
    notes = select(VisitNote).where(VisitNote.visit_id == visit.id)
    book = pack.playbook
    return ReviewView(
        visit=visit,
        report=report,
        tasks=list(tasks),
        insights=insight_views(session, list(session.scalars(query))),
        notes=list(session.scalars(notes.order_by(VisitNote.created_at, VisitNote.id))),
        kinds=[Choice(key, _label(key)) for key in book.kinds],
        categories=[Choice(key, category.name) for key, category in book.categories.items()],
        severities=[Choice(key, _label(key)) for key in book.severities],
    )


def insight_views(session: Session, insights: list[Insight]) -> list[InsightView]:
    """Each insight with its quotes, its teams and its links' names: one query each, for all."""
    ids = [insight.id for insight in insights]
    quotes: dict[str, list[QuoteView]] = {}
    for row in session.scalars(
        select(Evidence).where(Evidence.insight_id.in_(ids)).order_by(Evidence.seq)
    ):
        text = row.quote_final if row.quote_final is not None else row.quote_llm
        found = QuoteView(row.seq, row.turn_id, row.speaker_role, text, row.match, row.start_ms)
        quotes.setdefault(row.insight_id, []).append(found)
    routes: dict[str, list[RouteView]] = {}
    for insight_id, slot, key, name, routed_by in session.execute(
        select(
            InsightRoute.insight_id,
            InsightRoute.slot,
            Team.key,
            Team.name,
            InsightRoute.routed_by,
        )
        .join(Team, Team.id == InsightRoute.team_id)
        .where(InsightRoute.insight_id.in_(ids))
        .order_by(InsightRoute.slot)
    ):
        routes.setdefault(insight_id, []).append(RouteView(slot, key, name, routed_by))
    linked = {i.sku_id for i in insights} | {i.competitor_id for i in insights}
    names = {
        item_id: f"{brand} {product}" if product else brand
        for item_id, brand, product in session.execute(
            select(CatalogItem.id, CatalogItem.brand, CatalogItem.product).where(
                CatalogItem.id.in_(linked - {None})
            )
        )
    }
    teams = {
        team_id: key
        for team_id, key in session.execute(
            select(Team.id, Team.key).where(Team.id.in_({i.ai_team_id for i in insights} - {None}))
        )
    }
    return [
        InsightView(
            insight=insight,
            sku=names.get(insight.sku_id),
            competitor=names.get(insight.competitor_id),
            ai_team=teams.get(insight.ai_team_id),
            quotes=quotes.get(insight.id, []),
            routes=routes.get(insight.id, []),
        )
        for insight in insights
    ]
