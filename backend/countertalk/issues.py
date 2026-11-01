"""Issues (batch 15; BRIEF D16): a team's confirmed insights grouped by likely cause.

An issue is one team's. Its key is the insight's category and the values of that category's
group_by fields (playbook.yaml), in that order, "-" for a missing one: the linked IDs (sku_id,
competitor_id, scheme_key), never the words said, and the shop's distributor, or its area when
it has none. Membership is per route (insight_routes.issue_id), so an insight sent to two teams
is in one issue of each. This module alone sets a route's issue_id; routing.py writes the rest.

Attaching, on confirm (review.confirm, after lock_visit) and when a team member routes a
confirmed insight (the inbox): issues are locked in (team_id, issue_key) order, the lock order
of CODING_GUIDELINES §2.3, so two transactions can't deadlock. For each route:
1. the open or in-progress issue with its team and key is joined;
2. else one resolved less than REOPEN_WINDOW_DAYS ago reopens, with an IssueEvent by the actor;
3. else a new one: INSERT … ON CONFLICT DO NOTHING on the partial unique index; if another
   transaction made it first, it's joined once that one commits.
Joining: the severity becomes the higher, the seen times widen, and a higher severity can only
bring the due date earlier. A new or reopened issue is due its severity's due_days from now.
Every change bumps the issue's version, for the inbox's compare-and-set (§2.3).
"""

import logging
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Select, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from countertalk import entities, limits
from countertalk.errors import Conflict
from countertalk.models import (
    ACTIVE_ISSUE,
    ACTIVE_ISSUE_SQL,
    Distributor,
    Insight,
    InsightRoute,
    Issue,
    IssueEvent,
    IssueEventField,
    IssueStatus,
    Shop,
    Visit,
    new_id,
    utc_now,
)
from countertalk.playbooks import OrgPack
from countertalk.reporting import log_buckets

log = logging.getLogger(__name__)

MISSING = "-"
# In the distributor's place, for a shop with none: its area.
AREA_PREFIX = "area:"
# Each key's tries: a try ends early only if the issue it saw changed before it could lock it.
ATTACH_TRIES = 3
HOW = ("created", "reopened", "joined")


@dataclass(frozen=True)
class Member:
    """A route about to join its team's issue, and what its key and dates come from."""

    route: InsightRoute
    insight: Insight
    shop: Shop
    # When its visit happened.
    seen_at: datetime


def key_fields(group_by: Iterable[str], insight: Insight, shop: Shop) -> dict[str, str | None]:
    """Each group_by field's value, in order: an ID, a scheme key, an area, or None."""
    values = {
        "sku": insight.sku_id,
        "competitor": insight.competitor_id,
        "scheme": insight.scheme_key,
        "distributor": shop.distributor_id or f"{AREA_PREFIX}{shop.area}",
        "area": shop.area,
    }
    return {field: values[field] for field in group_by}


def issue_key(category: str, fields: dict[str, str | None]) -> str:
    """'payments|<distributor id>'. Unambiguous: a value's own | and % are escaped, and a value
    that is just - too."""
    return "|".join([category, *(_part(value) for value in fields.values())])


def _part(value: str | None) -> str:
    if value is None:
        return MISSING
    escaped = value.replace("%", "%25").replace("|", "%7C")
    return "%2D" if escaped == MISSING else escaped


def title_of(
    pack: OrgPack, category: str, fields: dict[str, str | None], names: dict[str, str]
) -> str:
    """'Credit and payments · North Traders (example)': the category's name and each value's
    label, from the catalog, the pack's schemes and the distributors. A missing value adds
    nothing."""
    schemes = {scheme.key: scheme.name for scheme in pack.schemes}
    parts = [pack.playbook.categories[category].name]
    for field, value in fields.items():
        if value is None:
            continue
        if field == "scheme":
            parts.append(schemes.get(value, value))
        elif field == "distributor" and value.startswith(AREA_PREFIX):
            parts.append(f"{value.removeprefix(AREA_PREFIX)} (no distributor)")
        elif field == "area":
            parts.append(value)
        else:
            parts.append(names.get(value, value))
    return " · ".join(parts)


def attach_visit(session: Session, pack: OrgPack, visit: Visit, actor: str) -> Counter[str]:
    """On confirm, after its lock_visit: each route of the visit's visible insights joins its
    team's issue. How many issues were created, reopened and joined."""
    shop = session.get_one(Shop, visit.shop_id)
    rows = session.execute(
        select(InsightRoute, Insight)
        .join(Insight, Insight.id == InsightRoute.insight_id)
        .where(Insight.visit_id == visit.id, Insight.visible.is_(True))
        .execution_options(populate_existing=True)
    ).all()
    members = [Member(route, insight, shop, visit.created_at) for route, insight in rows]
    done = attach(session, pack, visit.org_id, actor, members)
    log.info("issues attached visit=%s %s", visit.id, log_buckets(done, HOW))
    return done


def attach(
    session: Session, pack: OrgPack, org_id: str, actor: str, members: Sequence[Member]
) -> Counter[str]:
    """Each member's route joins its team's issue, in the caller's transaction, issues locked
    in plan()'s order. How many issues were created, reopened and joined: each counted once,
    however many routes it took, and each changed one's version bumped once."""
    planned = plan(pack, members)
    names = _names(session, org_id, [fields for _, _, fields, _ in planned])
    touched: dict[str, tuple[Issue, str]] = {}
    now = utc_now()
    for team_id, key, fields, member in planned:
        issue, how = _issue_for(
            session, pack, actor, (org_id, team_id, key), fields, member, names, now
        )
        if how != "created":
            _join(issue, member, pack, now)
        member.route.issue_id = issue.id
        touched.setdefault(issue.id, (issue, how))
    for issue, how in touched.values():
        if how != "created":
            issue.version += 1
    return Counter(how for _, how in touched.values())


Plan = tuple[str, str, dict[str, str | None], Member]


def plan(pack: OrgPack, members: Sequence[Member]) -> list[Plan]:
    """Each member's team, key and key fields, in (team_id, issue_key) order: the lock order of
    §2.3, so two transactions attaching to the same issues can't deadlock. Two teams can share a
    key, so the team comes first. A category or severity the org's playbook no longer has is
    refused (409), for the rep to edit first."""
    book = pack.playbook
    planned = []
    for member in members:
        category, severity = member.insight.category, member.insight.severity
        if category not in book.categories or severity not in book.severities:
            raise Conflict(
                "playbook_changed",
                "A finding has a category or severity the playbook no longer has: edit it first",
            )
        fields = key_fields(book.categories[category].group_by, member.insight, member.shop)
        planned.append((member.route.team_id, issue_key(category, fields), fields, member))
    return sorted(planned, key=lambda each: (each[0], each[1]))


def _issue_for(
    session: Session,
    pack: OrgPack,
    actor: str,
    where: tuple[str, str, str],
    fields: dict[str, str | None],
    member: Member,
    names: dict[str, str],
    now: datetime,
) -> tuple[Issue, str]:
    """The issue the member joins, locked, and how it was found: where is (org, team, key)."""
    org_id, team_id, key = where
    same = (Issue.org_id == org_id, Issue.team_id == team_id, Issue.issue_key == key)
    for _ in range(ATTACH_TRIES):
        active = session.scalars(_locked(select(Issue).where(*same, _active()))).one_or_none()
        if active is not None:
            return active, "joined"
        since = now - timedelta(days=limits.REOPEN_WINDOW_DAYS)
        recent = select(Issue).where(
            *same, Issue.status == IssueStatus.RESOLVED, Issue.resolved_at > since
        )
        resolved = session.scalars(
            _locked(recent.order_by(Issue.resolved_at.desc()).limit(1))
        ).one_or_none()
        if resolved is not None:
            _reopen(session, resolved, actor, member, pack, now)
            return resolved, "reopened"
        severity = member.insight.severity
        made = session.scalar(
            insert(Issue)
            .values(
                id=new_id(),
                org_id=org_id,
                team_id=team_id,
                category=member.insight.category,
                issue_key=key,
                key_fields=fields,
                title_en=title_of(pack, member.insight.category, fields, names),
                severity=severity,
                status=IssueStatus.OPEN.value,
                due_at=now + _due(pack, severity),
                first_seen_at=member.seen_at,
                last_seen_at=member.seen_at,
                reopened_count=0,
                version=1,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["org_id", "team_id", "issue_key"],
                index_where=text(ACTIVE_ISSUE_SQL),
            )
            .returning(Issue.id)
        )
        if made is not None:
            return session.scalars(_locked(select(Issue).where(Issue.id == made))).one(), "created"
        # Another transaction made it first, and has committed: it's joined on the next try.
    raise Conflict("issue_busy", "Its issue kept changing while it was being filed: try again")


def _active() -> Any:
    return Issue.status.in_([status.value for status in ACTIVE_ISSUE])


def _locked(query: Select[tuple[Issue]]) -> Select[tuple[Issue]]:
    """FOR UPDATE, read fresh (R6)."""
    return query.with_for_update().execution_options(populate_existing=True)


def _due(pack: OrgPack, severity: str) -> timedelta:
    return timedelta(days=pack.playbook.severities[severity].due_days)


def _reopen(
    session: Session, issue: Issue, actor: str, member: Member, pack: OrgPack, now: datetime
) -> None:
    """A fresh start: the severity and due date are the new insight's, not the resolved
    ones'."""
    session.add(
        IssueEvent(
            org_id=issue.org_id,
            issue_id=issue.id,
            actor_id=actor,
            field=IssueEventField.STATUS,
            from_value=IssueStatus.RESOLVED.value,
            to_value=IssueStatus.OPEN.value,
        )
    )
    issue.status = IssueStatus.OPEN.value
    issue.resolved_at = None
    issue.resolution_note = None
    issue.reopened_count += 1
    issue.severity = member.insight.severity
    issue.due_at = now + _due(pack, issue.severity)


def _join(issue: Issue, member: Member, pack: OrgPack, now: datetime) -> None:
    order = list(pack.playbook.severities)
    severity = member.insight.severity
    if order.index(severity) < order.index(issue.severity):
        issue.severity = severity
        issue.due_at = min(issue.due_at, now + _due(pack, severity))
    issue.first_seen_at = min(issue.first_seen_at, member.seen_at)
    issue.last_seen_at = max(issue.last_seen_at, member.seen_at)


def _names(
    session: Session, org_id: str, all_fields: Sequence[dict[str, str | None]]
) -> dict[str, str]:
    """The catalog items' and distributors' names the keys refer to, for titles."""
    ids = {
        value
        for fields in all_fields
        for field, value in fields.items()
        if field in ("sku", "competitor", "distributor")
        and value is not None
        and not value.startswith(AREA_PREFIX)
    }
    if not ids:
        return {}
    names = entities.item_names(session, org_id, ids)
    distributors = select(Distributor.id, Distributor.name).where(
        Distributor.org_id == org_id, Distributor.id.in_(ids)
    )
    names.update(dict(session.execute(distributors).all()))
    return names
