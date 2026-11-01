"""Routing (batch 12; BRIEF D7): the AI suggests a team, code decides.

decide() is the rule: a plain function, with no session and no model call, so the extract job,
batch 14's re-route and batch 13's eval all use the same one. For slot 1, the main team:
1. the table and the AI agree, or the AI has no suggestion: the table's team;
2. the table has no team (an unmapped category) and the AI suggests one: the AI's, for a team
   member to accept;
3. they disagree: the table's, and the card offers the AI's as Move or Also send;
4. neither has a team: no route, so the insight is in triage.
Slot 2 is the category's override, when the insight is at least as severe as it asks. With no
main team it takes slot 1, and an AI suggesting the same team doesn't make it a suggestion: the
table decided. The AI's team key, if it isn't one of the org's teams (another org's, say), is
never used: it's recorded as unknown. A pack team the org hasn't got (a team added since its
import) is skipped too, so the insight falls to the AI's team or triage; never a guess.

An unverified insight is never routed (BRIEF D9).

Only this module writes InsightRoute (an architecture test), and every change writes its
RoutingDecision rows in the same transaction.
"""

import logging
from collections.abc import Collection
from dataclasses import dataclass
from itertools import count
from typing import Literal

from sqlalchemy import delete, exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from countertalk import issues, rows
from countertalk.db import constraint_name
from countertalk.errors import Conflict, NotFound
from countertalk.models import (
    TRIAGE_RULE,
    Insight,
    InsightRoute,
    RoutedBy,
    RoutingAction,
    RoutingDecision,
    Team,
    parse_id,
)
from countertalk.playbooks import OrgPack, Route, routing_table

log = logging.getLogger(__name__)

# The constraint that caps an insight's teams (models.InsightRoute).
CAP_CONSTRAINT = "ck_insight_routes_slot"


@dataclass(frozen=True)
class Table:
    """An org's routing, from its pack: each category's team and override, and its severities,
    highest first."""

    routes: dict[str, Route]
    severities: tuple[str, ...]


def table_of(pack: OrgPack) -> Table:
    return Table({r.category: r for r in routing_table(pack)}, tuple(pack.playbook.severities))


@dataclass(frozen=True)
class Choice:
    slot: int
    team: str
    routed_by: RoutedBy
    # agreed, table_only, ai_filled_gap, disagreed or override:<category>
    rule: str


@dataclass(frozen=True)
class Decision:
    # Empty: triage.
    routes: tuple[Choice, ...]
    ai_team_key_unknown: str | None


def decide(
    category: str, severity: str, ai_team: str | None, table: Table, team_keys: Collection[str]
) -> Decision:
    unknown = ai_team if ai_team is not None and ai_team not in team_keys else None
    ai = None if unknown is not None else ai_team
    route = table.routes.get(category)
    team = route.team if route is not None and route.team in team_keys else None
    override = route.override if route is not None else None
    fired = (
        override.team
        if override is not None
        and override.team in team_keys
        and _at_least(severity, override.min_severity, table.severities)
        else None
    )
    routes: list[Choice] = []
    if team is not None:
        rule = "table_only" if ai is None else "agreed" if ai == team else "disagreed"
        routes.append(Choice(1, team, RoutedBy.TABLE, rule))
    elif ai is not None and ai != fired:
        routes.append(Choice(1, ai, RoutedBy.AI, "ai_filled_gap"))
    if fired is not None and fired not in {choice.team for choice in routes}:
        routes.append(Choice(len(routes) + 1, fired, RoutedBy.OVERRIDE, f"override:{category}"))
    return Decision(tuple(routes), unknown)


def _at_least(severity: str, minimum: str, highest_first: tuple[str, ...]) -> bool:
    if severity not in highest_first or minimum not in highest_first:
        return False
    return highest_first.index(severity) <= highest_first.index(minimum)


Routed = Literal["routed", "triage", "unverified", "already"]


def route_new(
    session: Session, insight: Insight, decision: Decision, team_ids: dict[str, str]
) -> Routed:
    """Gives an insight the routes decided, each with its history row, or a triage row, in the
    caller's transaction. An insight routed before, or unverified, is left as it is (§2.5, D9).
    What it did."""
    insight = _lock(session, insight.org_id, insight.id)
    if not insight.verified:
        return "unverified"
    routed = exists().where(RoutingDecision.insight_id == insight.id)
    if session.scalar(select(routed)):
        log.info(
            "routing skipped, already routed visit=%s insight=%s", insight.visit_id, insight.id
        )
        return "already"
    return _write(session, insight, decision, team_ids, None)


def team_ids(session: Session, org_id: str) -> dict[str, str]:
    """The org's teams, key: id: what decide() and the route writers take (R1)."""
    query = select(Team.key, Team.id).where(Team.org_id == org_id)
    return {key: team_id for key, team_id in session.execute(query)}


def team_keys(session: Session, org_id: str) -> dict[str, str]:
    """The org's teams the other way round, id: key, for showing a route's team (R1)."""
    return {team_id: key for key, team_id in team_ids(session, org_id).items()}


def ai_team_key(session: Session, insight: Insight, teams: dict[str, str]) -> str | None:
    """The AI's team for an insight being routed again: its ai_team_id's key, or the unknown
    key its first routing recorded, so a re-route records it again."""
    for key, team_id in teams.items():
        if team_id == insight.ai_team_id:
            return key
    query = (
        select(RoutingDecision.ai_team_key_unknown)
        .where(
            RoutingDecision.insight_id == insight.id,
            RoutingDecision.ai_team_key_unknown.is_not(None),
        )
        .order_by(RoutingDecision.created_at)
        .limit(1)
    )
    return session.scalar(query)


def reroute(
    session: Session, insight: Insight, decision: Decision, team_ids: dict[str, str], actor: str
) -> Routed:
    """A rep's category or severity change (batch 14): the insight's routes are replaced by the
    rule's for its new values, each with a `reroute` history row naming the rep as well as the
    rule decide() applied. Under the insight's lock, in the caller's transaction. An unverified
    insight is never routed (D9).

    Only the rule's routes are ever replaced: edits happen in review, before a team member can
    route an insight by hand (batch 15's inbox shows confirmed visits), and one routed by hand
    is refused rather than lose its route without a trace."""
    insight = _lock(session, insight.org_id, insight.id)
    if not insight.verified:
        return "unverified"
    by_hand = select(InsightRoute.id).where(
        InsightRoute.insight_id == insight.id, InsightRoute.routed_by == RoutedBy.PERSON
    )
    if session.scalar(by_hand.limit(1)) is not None:
        raise Conflict("routed_by_hand", "A team member has routed it by hand: change it there")
    session.execute(delete(InsightRoute).where(InsightRoute.insight_id == insight.id))
    return _write(session, insight, decision, team_ids, actor)


def _write(
    session: Session,
    insight: Insight,
    decision: Decision,
    team_ids: dict[str, str],
    rerouted_by: str | None,
) -> Routed:
    """The decided routes, each with its history row, or a triage row: the rule's (`route`,
    `override`), or a rep's re-route (`reroute`, decided_by the rep)."""
    history = {
        "org_id": insight.org_id,
        "insight_id": insight.id,
        "extraction_id": insight.extraction_id,
        "decided_by": rerouted_by,
    }
    unknown = decision.ai_team_key_unknown
    if not decision.routes:
        action = RoutingAction.REROUTE if rerouted_by else RoutingAction.ROUTE
        session.add(
            RoutingDecision(**history, action=action, rule=TRIAGE_RULE, ai_team_key_unknown=unknown)
        )
        return "triage"
    for choice in decision.routes:
        team_id = team_ids[choice.team]
        session.add(
            InsightRoute(
                org_id=insight.org_id,
                insight_id=insight.id,
                slot=choice.slot,
                team_id=team_id,
                routed_by=choice.routed_by,
            )
        )
        if rerouted_by is not None:
            action = RoutingAction.REROUTE
        elif choice.routed_by == RoutedBy.OVERRIDE:
            action = RoutingAction.OVERRIDE
        else:
            action = RoutingAction.ROUTE
        session.add(
            RoutingDecision(
                **history,
                slot=choice.slot,
                team_id=team_id,
                action=action,
                rule=choice.rule,
                ai_team_key_unknown=unknown if choice.slot == 1 else None,
            )
        )
    return "routed"


# --- a team member's routing, after confirm (the inbox, batch 15) ----------------------------


@dataclass(frozen=True)
class Seen:
    """The route a team member acted on, as they read it: a change names it, and gets 409
    route_changed if it has moved on (compare-and-set, §2.3)."""

    slot: int
    version: int


def accept(session: Session, org_id: str, insight_id: str, actor: str, seen: Seen) -> InsightRoute:
    """The AI's suggestion, accepted: the route is a person's now, so "suggested by AI" goes."""
    insight = _shown(session, org_id, insight_id)
    route = _seen(_routes(session, insight), seen)
    if route.routed_by != RoutedBy.AI:
        raise Conflict("nothing_to_accept", "It isn't the AI's suggestion")
    route.routed_by = RoutedBy.PERSON
    route.version += 1
    _person(session, insight, route.slot, route.team_id, RoutingAction.ACCEPT, actor)
    return route


def move(
    session: Session,
    pack: OrgPack,
    org_id: str,
    insight_id: str,
    team_id: str,
    actor: str,
    seen: Seen,
) -> InsightRoute:
    """The route goes to another team: out of its issue, into that team's (issues.py, in §2.3's
    order, after the insight's lock), and a person's now."""
    insight = _shown(session, org_id, insight_id)
    team = _team(session, org_id, team_id)
    routes = _routes(session, insight)
    route = _seen(routes, seen)
    _not_sent_to(routes, team)
    left = issues.detach(route)
    route.team_id = team
    route.routed_by = RoutedBy.PERSON
    route.version += 1
    _person(session, insight, route.slot, team, RoutingAction.MOVE, actor)
    issues.attach_route(session, pack, actor, route, insight, leaving=left)
    return route


def also_send(
    session: Session,
    pack: OrgPack,
    org_id: str,
    insight_id: str,
    team_id: str,
    actor: str,
    seen: Seen,
) -> InsightRoute:
    """The insight also goes to another team: the first free slot, filed in that team's issue.
    409 team_limit when it has as many teams as the cap allows (two people at once included),
    already_routed when that team has it, not_routed when it's in triage (that's a triage
    assignment)."""
    insight = _shown(session, org_id, insight_id)
    team = _team(session, org_id, team_id)
    routes = _routes(session, insight)
    if not routes:
        raise Conflict("not_routed", "It has no team yet: give it one from triage first")
    _seen(routes, seen)
    _not_sent_to(routes, team)
    slot = next(n for n in count(1) if n not in routes)
    route = InsightRoute(
        org_id=org_id, insight_id=insight.id, slot=slot, team_id=team, routed_by=RoutedBy.PERSON
    )
    try:
        with session.begin_nested():
            session.add(route)
            session.flush()
    except IntegrityError as error:
        if constraint_name(error) != CAP_CONSTRAINT:
            raise
        raise Conflict("team_limit", "It already has as many teams as it can have") from None
    _person(session, insight, slot, team, RoutingAction.ALSO_SEND, actor)
    issues.attach_route(session, pack, actor, route, insight)
    return route


def assign_from_triage(
    session: Session, pack: OrgPack, org_id: str, insight_id: str, team_id: str, actor: str
) -> InsightRoute:
    """A team for an insight the rule left in triage: slot 1, filed in that team's issue. The
    request read it with no routes; one that has any now is 409 already_routed, so two people
    can't put it in two teams (compare-and-set, §2.3)."""
    insight = _shown(session, org_id, insight_id)
    team = _team(session, org_id, team_id)
    if _routes(session, insight):
        raise Conflict("already_routed", "Someone gave it a team meanwhile: reload it")
    route = InsightRoute(
        org_id=org_id, insight_id=insight.id, slot=1, team_id=team, routed_by=RoutedBy.PERSON
    )
    session.add(route)
    _person(session, insight, 1, team, RoutingAction.TRIAGE_ASSIGN, actor)
    issues.attach_route(session, pack, actor, route, insight)
    return route


def _shown(session: Session, org_id: str, insight_id: str) -> Insight:
    """The insight, locked: only a confirmed visit's shown insight is routed by hand."""
    insight = _lock(session, org_id, insight_id)
    if not insight.visible:
        raise Conflict("insight_not_confirmed", "Only a confirmed visit's findings go to teams")
    return insight


def _routes(session: Session, insight: Insight) -> dict[int, InsightRoute]:
    """Its routes by slot, read fresh under its lock (R6)."""
    query = select(InsightRoute).where(InsightRoute.insight_id == insight.id)
    found = session.scalars(query.execution_options(populate_existing=True))
    return {route.slot: route for route in found}


def _seen(routes: dict[int, InsightRoute], seen: Seen) -> InsightRoute:
    route = routes.get(seen.slot)
    if route is None or route.version != seen.version:
        raise Conflict("route_changed", "Someone routed it meanwhile: reload it")
    return route


def _not_sent_to(routes: dict[int, InsightRoute], team: str) -> None:
    if team in {route.team_id for route in routes.values()}:
        raise Conflict("already_routed", "It's already sent to that team")


def _team(session: Session, org_id: str, team_id: str) -> str:
    query = select(Team.id).where(Team.org_id == org_id, Team.id == parse_id(team_id))
    team = session.scalar(query)
    if team is None:
        raise NotFound("team_not_found", "There's no such team")
    return team


def _person(
    session: Session, insight: Insight, slot: int, team: str, action: RoutingAction, actor: str
) -> None:
    """A person's routing decision's history row."""
    session.add(
        RoutingDecision(
            org_id=insight.org_id,
            insight_id=insight.id,
            slot=slot,
            team_id=team,
            action=action,
            decided_by=actor,
            extraction_id=insight.extraction_id,
        )
    )


def _lock(session: Session, org_id: str, insight_id: str) -> Insight:
    """SELECT … FOR UPDATE on one insight of this org, read fresh (R6). Another org's is 404."""
    missing = NotFound("insight_not_found", "There's no such insight")
    return rows.find_one(session, Insight, org_id, insight_id, missing, for_update=True)
