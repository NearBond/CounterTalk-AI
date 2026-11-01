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


def also_send(session: Session, org_id: str, insight_id: str, team_id: str, user_id: str) -> int:
    """A team member also sends a routed insight to another team: the first free slot, under
    the insight's lock. 409 team_limit when it has as many teams as the cap allows, already_routed
    when that team has it, not_routed when it's in triage (that's a triage assignment). Its
    slot."""
    insight = _lock(session, org_id, insight_id)
    query = select(Team.id).where(Team.org_id == org_id, Team.id == parse_id(team_id))
    team = session.scalar(query)
    if team is None:
        raise NotFound("team_not_found", "There's no such team")
    routed = select(InsightRoute.slot, InsightRoute.team_id).where(
        InsightRoute.insight_id == insight.id
    )
    routes = {slot: routed_to for slot, routed_to in session.execute(routed)}
    if not routes:
        raise Conflict("not_routed", "It has no team yet: give it one from triage first")
    if team in routes.values():
        raise Conflict("already_routed", "It's already sent to that team")
    slot = next(n for n in count(1) if n not in routes)
    try:
        with session.begin_nested():
            session.add(
                InsightRoute(
                    org_id=org_id,
                    insight_id=insight.id,
                    slot=slot,
                    team_id=team,
                    routed_by=RoutedBy.PERSON,
                )
            )
            session.flush()
    except IntegrityError as error:
        if constraint_name(error) != CAP_CONSTRAINT:
            raise
        raise Conflict("team_limit", "It already has as many teams as it can have") from None
    session.add(
        RoutingDecision(
            org_id=org_id,
            insight_id=insight.id,
            slot=slot,
            team_id=team,
            action=RoutingAction.ALSO_SEND,
            decided_by=user_id,
            extraction_id=insight.extraction_id,
        )
    )
    return slot


def _lock(session: Session, org_id: str, insight_id: str) -> Insight:
    """SELECT … FOR UPDATE on one insight of this org, read fresh (R6). Another org's is 404."""
    found = parse_id(insight_id)
    query = select(Insight).where(Insight.id == found, Insight.org_id == org_id)
    insight = None
    if found is not None:
        query = query.with_for_update().execution_options(populate_existing=True)
        insight = session.scalars(query).one_or_none()
    if insight is None:
        raise NotFound("insight_not_found", "There's no such insight")
    return insight
