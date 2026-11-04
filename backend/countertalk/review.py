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
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from countertalk import entities, issues, limits, roles, routing, rows, verify, visits
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
    Issue,
    OrderItem,
    Report,
    RoutedBy,
    Task,
    Team,
    Turn,
    Visit,
    VisitNote,
    VisitStatus,
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


def missing_insight() -> NotFound:
    """A fresh one each time (errors_are_raised_fresh)."""
    return NotFound("insight_not_found", "There's no such insight")


def _locked_insight(session: Session, org_id: str, insight_id: str) -> tuple[Visit, Insight]:
    """The insight, read fresh once its visit is locked (R6). Another org's is 404."""
    found = parse_id(insight_id)
    where = (Insight.id == found, Insight.org_id == org_id)
    visit_id = session.scalar(select(Insight.visit_id).where(*where)) if found else None
    if visit_id is None:
        raise missing_insight()
    visit = locked_in_review(session, org_id, visit_id)
    query = select(Insight).where(*where).execution_options(populate_existing=True)
    insight = session.scalars(query).one_or_none()
    if insight is None:  # deleted while the lock was waited for
        raise missing_insight()
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


def _suggestion(
    session: Session, pack: OrgPack, insight: Insight, teams: dict[str, str] | None = None
) -> tuple[routing.Decision, dict[str, str]]:
    """The rule's routes for the insight's category and severity, and its AI team, with the
    org's teams (key: id; read unless given)."""
    teams = teams if teams is not None else routing.team_ids(session, insight.org_id)
    ai = routing.ai_team_key(session, insight, teams)
    table = routing.table_of(pack)
    return routing.decide(insight.category, insight.severity, ai, table, teams), teams


def _route(session: Session, pack: OrgPack, insight: Insight, actor: str | None) -> None:
    """The rule's routes, given to a new insight, or replacing an edited one's (a `reroute` by
    the actor): a rep's own choice goes, for the new suggestion."""
    decision, teams = _suggestion(session, pack, insight)
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


def choose_teams(
    session: Session,
    pack_for: Callable[[], OrgPack],
    org_id: str,
    actor: str,
    insight_id: str,
    chosen: list[str],
    seen: int,
) -> Insight:
    """The visit's rep's own teams for a finding, in review (batch F1; routing.choose): the
    rule's routes are the suggestion, and the override's team stays."""
    visit, insight = _locked_insight(session, org_id, insight_id)
    decision, teams = _suggestion(session, pack_for(), insight)
    routing.choose(session, insight, decision, teams, chosen, actor, seen)
    log.info("teams chosen visit=%s insight=%s teams=%d", visit.id, insight.id, len(chosen))
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
    insight_id: str,
    turn_id: int,
    kind: str,
    category: str,
    severity: str,
    summary_en: str,
) -> Insight:
    """A finding the AI missed, quoting the whole of a turn of the visit's current transcript:
    exact, playable, and routed by the table (it has no AI team). Its ID is the client's, so
    sending it again adds nothing and returns the one added (§2.5)."""
    visit = locked_in_review(session, org_id, visit_id)
    again = session.get(Insight, insight_id)
    if again is not None:
        if again.visit_id == visit.id and again.added_by_rep:
            return again
        raise Conflict("insight_exists", "That ID is another insight's")
    found = {
        "kind": _one_of(kind, pack.playbook.kinds, "kind"),
        "category": _one_of(category, pack.playbook.categories, "category"),
        "severity": _one_of(severity, pack.playbook.severities, "severity"),
        "summary_en": _text(summary_en),
    }
    turn = session.get(Turn, (visit.transcript_id, turn_id)) if visit.transcript_id else None
    if turn is None:
        raise NotFound("turn_not_found", "That line isn't in this visit's transcript")
    # Only the shopkeeper's words become insights (the playbook's roles).
    role = roles.saved_roles(session, turn.transcript_id).get(turn.speaker_label, (None,))[0]
    if role != roles.SHOPKEEPER:
        raise Invalid("not_shopkeeper_turn", "Pick a line the shopkeeper said")
    insight = Insight(
        id=insight_id,
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


def add_note(
    session: Session, org_id: str, author: str, visit_id: str, note_id: str, text: str
) -> VisitNote:
    """For the visit's record only: never routed, never shown to teams. Its ID is the client's,
    so sending it again adds nothing and returns the one added (§2.5)."""
    visit = locked_in_review(session, org_id, visit_id)
    again = session.get(VisitNote, note_id)
    if again is not None:
        if again.visit_id == visit.id:
            return again
        raise Conflict("note_exists", "That ID is another note's")
    body = text.strip()
    if not body:
        raise Invalid("note_empty", "The note is empty")
    if len(body) > limits.NOTE_MAX_CHARS:
        raise Invalid("note_too_long", f"A note is at most {limits.NOTE_MAX_CHARS} characters")
    note = VisitNote(id=note_id, org_id=org_id, visit_id=visit.id, author_id=author, text=body)
    session.add(note)
    session.flush()
    log.info("note added visit=%s note=%s", visit.id, note.id)
    return note


def confirm(
    session: Session, pack_for: Callable[[], OrgPack], org_id: str, actor: str, visit_id: str
) -> Visit:
    """Review to confirmed: the verified insights become visible, each of their routes joins
    its team's issue (batch 15, issues.py: the visit's lock, then the issues'), and the visit is
    locked against edits. Confirming again returns it as it is (§2.5)."""
    visit = visits.lock_visit(session, org_id, visit_id)
    if visit.status == VisitStatus.CONFIRMED:
        return visit
    _require_review(visit)
    visits.set_status(visit, VisitStatus.CONFIRMED)
    session.execute(
        update(Insight).where(Insight.visit_id == visit.id).values(visible=Insight.verified)
    )
    issues.attach_visit(session, pack_for(), visit, actor)
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
    team_id: str
    team_key: str
    team_name: str
    routed_by: str
    # What a team member's change names (compare-and-set, batch 15).
    version: int
    # Its team's issue, once confirmed: its status and resolution note are the answer the rep
    # takes back to the shop. None for a route confirmed before issues existed.
    issue_id: str | None
    issue_status: str | None
    resolution_note: str | None


@dataclass(frozen=True)
class InsightView:
    insight: Insight
    sku: str | None
    competitor: str | None
    ai_team: str | None
    quotes: list[QuoteView]
    routes: list[RouteView]
    # Batch F1: the rule's main team (its suggestion; None for triage), whether the rep chose
    # otherwise or left it for the manager, and what a rep's choice names. In review, the
    # category override's team, which the rep can't take off (review_of sets it).
    locked_team_id: str | None = None
    suggested_team: str | None = None
    chosen_by_rep: bool = False
    left_by_rep: bool = False
    routing_version: int = 0


@dataclass(frozen=True)
class OrderItemView:
    seq: int
    name: str
    quantity: float | None
    unit: str | None
    # The catalog item's name the name links to, if it does.
    sku: str | None


@dataclass(frozen=True)
class Choice:
    key: str
    label: str


@dataclass(frozen=True)
class ReviewView:
    visit: Visit
    report: Report | None
    tasks: list[Task]
    order_items: list[OrderItemView]
    insights: list[InsightView]
    notes: list[VisitNote]
    # Each speaker label's saved role: the add-a-finding picker shows it.
    speaker_roles: dict[str, str]
    kinds: list[Choice]
    categories: list[Choice]
    severities: list[Choice]
    # The org's teams, for the rep's team picker (batch F1).
    teams: list[Team]


def team_part(found: ReviewView, team_id: str) -> ReviewView:
    """What a team member sees of a visit's review (access.visit): only the insights routed to
    their team, each with only that team's route; none of the rep's notes, report, tasks or
    order."""
    insights = [
        replace(view, routes=[r for r in view.routes if r.team_id == team_id])
        for view in found.insights
        if any(r.team_id == team_id for r in view.routes)
    ]
    return replace(found, report=None, tasks=[], order_items=[], insights=insights, notes=[])


def category_choices(pack: OrgPack) -> list[Choice]:
    """The playbook's categories, with their names, in its order: the pickers' and filters'."""
    return [Choice(key, category.name) for key, category in pack.playbook.categories.items()]


def _label(key: str) -> str:
    return key.replace("_", " ").capitalize()


def review_of(
    session: Session, pack_for: Callable[[], OrgPack], org_id: str, visit_id: str
) -> ReviewView:
    """What the review page shows: the report, tasks, order, insights with their quotes and
    teams, notes, and the playbook's keys for its pickers. Another org's visit is 404, before
    the pack is loaded. Read in one snapshot (a read-only session's)."""
    visit = visits.get_visit(session, org_id, visit_id)
    pack = pack_for()
    report = session.get(Report, visit.id)
    tasks = session.scalars(select(Task).where(Task.visit_id == visit.id).order_by(Task.seq))
    query = select(Insight).where(Insight.visit_id == visit.id).order_by(Insight.seq)
    notes = select(VisitNote).where(VisitNote.visit_id == visit.id)
    # Each item with its product's name, in the same query.
    order = (
        select(OrderItem, CatalogItem.brand, CatalogItem.product)
        .outerjoin(CatalogItem, CatalogItem.id == OrderItem.sku_id)
        .where(OrderItem.visit_id == visit.id)
        .order_by(OrderItem.seq)
    )
    book = pack.playbook
    return ReviewView(
        visit=visit,
        report=report,
        tasks=list(tasks),
        order_items=[
            OrderItemView(
                seq=item.seq,
                name=item.name,
                quantity=None if item.quantity is None else float(item.quantity),
                unit=item.unit,
                sku=None if brand is None else entities.item_name(brand, product),
            )
            for item, brand, product in session.execute(order)
        ],
        insights=_with_locks(session, pack, insight_views(session, list(session.scalars(query)))),
        notes=list(session.scalars(notes.order_by(VisitNote.created_at, VisitNote.id))),
        speaker_roles={
            label: role
            for label, (role, _) in (
                roles.saved_roles(session, visit.transcript_id) if visit.transcript_id else {}
            ).items()
        },
        kinds=[Choice(key, _label(key)) for key in book.kinds],
        categories=category_choices(pack),
        severities=[Choice(key, _label(key)) for key in book.severities],
        teams=list(session.scalars(select(Team).where(Team.org_id == org_id).order_by(Team.name))),
    )


def _with_locks(session: Session, pack: OrgPack, views: list[InsightView]) -> list[InsightView]:
    """Each verified insight's locked team: its category override's, as the rule decides it now
    (what the rep's choice is checked against)."""
    teams = routing.team_ids(session, views[0].insight.org_id) if views else {}
    shown = []
    for view in views:
        locked = None
        if view.insight.verified:
            decision, _ = _suggestion(session, pack, view.insight, teams)
            overridden = [c.team for c in decision.routes if c.routed_by == RoutedBy.OVERRIDE]
            locked = teams[overridden[0]] if overridden else None
        shown.append(replace(view, locked_team_id=locked))
    return shown


def review_view(session: Session, pack: OrgPack, insight: Insight) -> InsightView:
    """One insight as the review page shows it, its locked team too: what a review edit's
    answer carries, so the next Edit shows the lock (batch F1)."""
    [view] = _with_locks(session, pack, insight_views(session, [insight]))
    return view


def view_of(session: Session, org_id: str, insight_id: str) -> InsightView:
    """One of the org's insights as the cards show it, read fresh."""
    missing = missing_insight()
    [view] = insight_views(session, [rows.find_one(session, Insight, org_id, insight_id, missing)])
    return view


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
    for row in session.execute(
        select(InsightRoute.insight_id, InsightRoute.slot, InsightRoute.team_id)
        .add_columns(Team.key, Team.name, InsightRoute.routed_by, InsightRoute.version)
        .add_columns(InsightRoute.issue_id, Issue.status, Issue.resolution_note)
        .join(Team, Team.id == InsightRoute.team_id)
        .outerjoin(Issue, Issue.id == InsightRoute.issue_id)
        .where(InsightRoute.insight_id.in_(ids))
        .order_by(InsightRoute.slot)
    ):
        routes.setdefault(row.insight_id, []).append(
            RouteView(
                slot=row.slot,
                team_id=row.team_id,
                team_key=row.key,
                team_name=row.name,
                routed_by=row.routed_by,
                version=row.version,
                issue_id=row.issue_id,
                issue_status=row.status,
                resolution_note=row.resolution_note,
            )
        )
    linked = [i.sku_id for i in insights] + [i.competitor_id for i in insights]
    names = entities.item_names(session, insights[0].org_id, linked) if insights else {}
    teams = {
        team_id: key
        for team_id, key in session.execute(
            select(Team.id, Team.key).where(Team.id.in_({i.ai_team_id for i in insights} - {None}))
        )
    }
    histories = routing.histories(session, ids)
    suggested_ids = {h.suggested_team_id for h in histories.values()} - {None}
    named = select(Team.id, Team.name).where(Team.id.in_(suggested_ids))
    team_names = {team_id: name for team_id, name in session.execute(named)}
    shown = []
    for insight in insights:
        history = histories.get(insight.id, routing.History(0, (), False, False))
        found = routes.get(insight.id, [])
        teams_now = [route.team_id for route in found]
        shown.append(
            InsightView(
                insight=insight,
                sku=names.get(insight.sku_id),
                competitor=names.get(insight.competitor_id),
                ai_team=teams.get(insight.ai_team_id),
                quotes=quotes.get(insight.id, []),
                routes=found,
                suggested_team=team_names.get(history.suggested_team_id or ""),
                # The rep's choice is the latest, and it isn't the suggestion.
                chosen_by_rep=history.latest_is_rep and teams_now != list(history.suggested),
                left_by_rep=history.left_by_rep and not found,
                routing_version=history.version,
            )
        )
    return shown
