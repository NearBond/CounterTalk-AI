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
2. else one resolved less than REOPEN_WINDOW_DAYS ago, before the insight was seen, reopens,
   with an IssueEvent by the actor; an insight seen before the resolve starts a new one;
3. else a new one: INSERT … ON CONFLICT DO NOTHING on the partial unique index; if another
   transaction made it first, it's joined once that one commits.
Joining: the severity becomes the higher, the seen times widen, and a higher severity can only
bring the due date earlier. A new or reopened issue, or an emptied one joined again, starts
fresh: due its severity's due_days from now. A route moving to another team leaves its old
issue, whose severity and seen times are worked out again from the routes it has left, both
issues locked in the same order. Every change bumps the issue's version, for the inbox's
compare-and-set (§2.3).
"""

import logging
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Select, exists, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from countertalk import entities, limits, rows
from countertalk.errors import Conflict, Forbidden, Invalid, NotFound
from countertalk.models import (
    ACTIVE_ISSUE,
    ACTIVE_ISSUE_SQL,
    Distributor,
    Insight,
    InsightRoute,
    Issue,
    IssueEvent,
    IssueEventField,
    IssueMergeSuggestion,
    IssueStatus,
    Shop,
    SuggestionStatus,
    Visit,
    new_id,
    parse_id,
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
            routed = select(exists().where(InsightRoute.issue_id == issue.id))
            _join(issue, member, pack, now, fresh=not session.scalar(routed))
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
            *same,
            Issue.status == IssueStatus.RESOLVED,
            Issue.resolved_at > since,
            Issue.resolved_at < member.seen_at,
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
            created_at=now,
        )
    )
    issue.status = IssueStatus.OPEN.value
    issue.resolved_at = None
    issue.resolution_note = None
    issue.reopened_count += 1
    issue.severity = member.insight.severity
    issue.due_at = now + _due(pack, issue.severity)


def _join(issue: Issue, member: Member, pack: OrgPack, now: datetime, *, fresh: bool) -> None:
    """Fresh: an issue every route had left starts again from this one (decision 7)."""
    severity = member.insight.severity
    if fresh:
        issue.severity = severity
        issue.due_at = now + _due(pack, severity)
        issue.first_seen_at = issue.last_seen_at = member.seen_at
        return
    if severity_rank(pack, severity) < severity_rank(pack, issue.severity):
        issue.severity = severity
        issue.due_at = min(issue.due_at, now + _due(pack, severity))
    issue.first_seen_at = min(issue.first_seen_at, member.seen_at)
    issue.last_seen_at = max(issue.last_seen_at, member.seen_at)


def severity_rank(pack: OrgPack, severity: str) -> int:
    """The playbook's severities, highest first from 0: the one ordering (R1). One it no longer
    has ranks last."""
    order = list(pack.playbook.severities)
    return order.index(severity) if severity in order else len(order)


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


def attach_route(
    session: Session,
    pack: OrgPack,
    actor: str,
    route: InsightRoute,
    insight: Insight,
    *,
    leaving: str | None = None,
) -> Counter[str]:
    """A route a team member made or moved after confirm (routing.py) joins its team's issue.
    Moved, it left `leaving` (detach): both issues are locked in §2.3's order, and the one it
    left is worked out again from the routes it has left, its version bumped."""
    visit = session.get_one(Visit, insight.visit_id)
    shop = session.get_one(Shop, visit.shop_id)
    member = Member(route, insight, shop, visit.created_at)
    old = (
        None
        if leaving is None
        else rows.find_one(session, Issue, insight.org_id, leaving, missing_issue())
    )
    [(team_id, key, _, _)] = plan(pack, [member])
    old_first = old is not None and (old.team_id, old.issue_key) < (team_id, key)
    if old is not None and old_first:
        old = session.scalars(_locked(select(Issue).where(Issue.id == old.id))).one()
    done = attach(session, pack, insight.org_id, actor, [member])
    if old is not None:
        if not old_first:
            old = session.scalars(_locked(select(Issue).where(Issue.id == old.id))).one()
        _refresh(session, pack, old)
    log.info(
        "issues attached visit=%s insight=%s %s left=%s",
        visit.id,
        insight.id,
        log_buckets(done, HOW),
        int(old is not None),
    )
    return done


def detach(route: InsightRoute) -> str | None:
    """A route leaving its issue, before it moves to another team (routing.move): the issue it
    left, for attach_route. The issue stays, hidden while it has no routes (with_routes)."""
    left, route.issue_id = route.issue_id, None
    return left


def _refresh(session: Session, pack: OrgPack, issue: Issue) -> None:
    """An issue a route left: its severity and seen times from the routes it has left, its due
    date as it was. One with none left keeps them, hidden until a new insight joins it."""
    rows = session.execute(
        select(Insight.severity, Visit.created_at)
        .join(InsightRoute, InsightRoute.insight_id == Insight.id)
        .join(Visit, Visit.id == Insight.visit_id)
        .where(InsightRoute.issue_id == issue.id)
    ).all()
    if rows:
        issue.severity = min(
            (severity for severity, _ in rows), key=lambda s: severity_rank(pack, s)
        )
        issue.first_seen_at = min(seen for _, seen in rows)
        issue.last_seen_at = max(seen for _, seen in rows)
    issue.version += 1


def with_routes() -> Any:
    """An issue with at least one route: the only ones the inbox shows and counts (R1, R10).
    One emptied by moves stays, and a new insight with its key brings it back."""
    return exists().where(InsightRoute.issue_id == Issue.id)


# --- a team member's changes (the inbox) -----------------------------------------------------

# By hand, an open or in-progress issue can go to these; resolved ones reopen only with a new
# insight, and merging is batch 15c's.
SETTABLE = (IssueStatus.OPEN, IssueStatus.IN_PROGRESS, IssueStatus.RESOLVED)


def change(
    session: Session,
    org_id: str,
    actor: str,
    issue_id: str,
    version: int,
    *,
    status: str | None = None,
    note: str | None = None,
    assign_to_me: bool = False,
    due_at: datetime | None = None,
) -> Issue:
    """A team member's change, compare-and-set on the version they read (§2.3): 409
    issue_changed if it moved on, nothing written. Assign to me sets the owner; only the owner
    changes the due date (403); resolving needs a note. An IssueEvent per field changed, and
    one version bump. Nothing changed, nothing written."""
    issue = _lock_issue(session, org_id, issue_id)
    if issue.version != version:
        raise Conflict("issue_changed", "Someone changed this issue meanwhile: reload it")
    if issue.status == IssueStatus.MERGED:
        raise Conflict("issue_merged", "It was merged into another issue: change that one")
    if issue.status not in ACTIVE_ISSUE:
        raise Conflict("issue_closed", "It's resolved: a new finding reopens it")
    if status is not None and status not in SETTABLE:
        raise Invalid("status_invalid", f"The status must be one of {', '.join(SETTABLE)}")
    if note is not None and status != IssueStatus.RESOLVED:
        raise Invalid("note_only_when_resolving", "A note goes with resolving it")
    # Every check first: a refused change writes nothing, whatever the caller does next. One
    # moment for all it writes: its events' and its resolve's (summaries read them, batch 15b).
    now = utc_now()
    owner = actor if assign_to_me else issue.owner_id
    move_due = due_at is not None and _due_change(issue, owner, actor, due_at)
    move_status = status is not None and status != issue.status
    resolution = _note(note) if move_status and status == IssueStatus.RESOLVED else None
    events: list[tuple[IssueEventField, str | None, str | None, str | None]] = []
    if owner != issue.owner_id:
        events.append((IssueEventField.OWNER, issue.owner_id, owner, None))
        issue.owner_id = owner
    if move_due and due_at is not None:
        events.append((IssueEventField.DUE_AT, issue.due_at.isoformat(), due_at.isoformat(), None))
        issue.due_at = due_at
    if move_status and status is not None:
        events.append((IssueEventField.STATUS, issue.status, status, resolution))
        issue.status = status
        issue.resolved_at = now if resolution is not None else None
        issue.resolution_note = resolution
    if not events:
        return issue
    session.add_all(
        IssueEvent(
            org_id=issue.org_id,
            issue_id=issue.id,
            actor_id=actor,
            field=field,
            from_value=before,
            to_value=after,
            note=text_note,
            created_at=now,
        )
        for field, before, after, text_note in events
    )
    issue.version += 1
    log.info("issue changed issue=%s fields=%s", issue.id, ",".join(e[0] for e in events))
    return issue


def merge(
    session: Session,
    pack: OrgPack,
    org_id: str,
    actor: str,
    into: tuple[str, int],
    merged: tuple[str, int],
) -> Issue:
    """A person merges one of a team's issues into another of the same category (batch 15c;
    never automatic): `into` and `merged` are each an issue's ID and the version read.

    Locks, in the order a move takes them (§2.3): the merged one's insights, then both issues in
    (team, key) order, then its routes. Then:
    - the merged one's routes move to `into`, so its insights, and the rep's page, follow;
    - `into` takes the higher severity, the earlier due date and the wider seen times;
    - the merged one is merged, with merged_into_id, and each has an IssueEvent.
    Merging into the same issue again changes nothing; one merged elsewhere is 409
    issue_merged. A version that moved on (a status change won the race) is 409 issue_changed.
    An issue with no findings left (all moved away) can't be merged: its severity and dates are
    stale. Returns `into`."""
    into_id, into_version = parse_id(into[0]), into[1]
    merged_id, merged_version = parse_id(merged[0]), merged[1]
    if into_id is None or merged_id is None:
        raise missing_issue()
    if into_id == merged_id:
        raise Invalid("merge_itself", "An issue can't be merged into itself")
    insights = select(InsightRoute.insight_id).where(InsightRoute.issue_id == merged_id)
    session.scalars(
        select(Insight.id)
        .where(Insight.org_id == org_id, Insight.id.in_(insights))
        .order_by(Insight.id)
        .with_for_update()
    ).all()
    found = {
        issue.id: issue
        for issue in session.scalars(
            _locked(
                select(Issue)
                .where(Issue.org_id == org_id, Issue.id.in_([into_id, merged_id]))
                .order_by(Issue.team_id, Issue.issue_key, Issue.id)
            )
        )
    }
    if len(found) != 2:
        raise missing_issue()
    target, source = found[into_id], found[merged_id]
    if source.status == IssueStatus.MERGED:
        if source.merged_into_id == target.id:
            return target  # merged already: the same request again changes nothing (§2.5)
        raise Conflict("issue_merged", "It was merged into another issue already")
    if target.status == IssueStatus.MERGED:
        raise Conflict("issue_merged", "It was merged into another issue: merge into that one")
    if (target.version, source.version) != (into_version, merged_version):
        raise Conflict("issue_changed", "Someone changed one of these issues meanwhile: reload")
    if (target.team_id, target.category) != (source.team_id, source.category):
        raise Invalid("merge_unlike", "Only one team's issues of one category can be merged")
    if target.status not in ACTIVE_ISSUE or source.status not in ACTIVE_ISSUE:
        raise Conflict("issue_closed", "Only open or in-progress issues can be merged")
    routes = session.scalars(
        select(InsightRoute).where(InsightRoute.issue_id == source.id).with_for_update()
    ).all()
    has_routes = select(exists().where(InsightRoute.issue_id == target.id))
    if not routes or not session.scalar(has_routes):
        raise Conflict("issue_empty", "That issue has no findings left: there's nothing to merge")
    now = utc_now()
    for route in routes:
        route.issue_id = target.id
    if severity_rank(pack, source.severity) < severity_rank(pack, target.severity):
        target.severity = source.severity
    target.due_at = min(target.due_at, source.due_at)
    target.first_seen_at = min(target.first_seen_at, source.first_seen_at)
    target.last_seen_at = max(target.last_seen_at, source.last_seen_at)
    events = [
        (source, IssueEventField.STATUS, source.status, IssueStatus.MERGED.value),
        (target, IssueEventField.MERGE, None, source.id),
    ]
    session.add_all(
        IssueEvent(
            org_id=org_id,
            issue_id=issue.id,
            actor_id=actor,
            field=field,
            from_value=before,
            to_value=after,
            created_at=now,
        )
        for issue, field, before, after in events
    )
    source.status, source.merged_into_id = IssueStatus.MERGED.value, target.id
    session.execute(
        update(IssueMergeSuggestion)
        .where(
            IssueMergeSuggestion.org_id == org_id,
            IssueMergeSuggestion.pair_key == pair_key(target.id, source.id),
            IssueMergeSuggestion.status == SuggestionStatus.OPEN,
        )
        .values(status=SuggestionStatus.MERGED, decided_by_id=actor, decided_at=now)
        .execution_options(synchronize_session=False)
    )
    target.version += 1
    source.version += 1
    log.info("issue merged issue=%s into=%s routes=%d", source.id, target.id, len(routes))
    return target


def pair_key(first: str, second: str) -> str:
    """Two issues' IDs, sorted: a merge suggestion's pair, whichever way round (batch 15c)."""
    return "|".join(sorted((first, second)))


def missing_issue() -> NotFound:
    """A fresh one each time: an error raised again keeps its old frames (R2)."""
    return NotFound("issue_not_found", "There's no such issue")


def _due_change(issue: Issue, owner: str | None, actor: str, due_at: datetime) -> bool:
    """Whether the due date changes: refused unless the actor owns it (after an assign to me in
    the same change) and it's a later time, with its time zone (R4)."""
    if due_at.tzinfo is None:
        raise Invalid("due_at_without_zone", "A due date needs its time zone")
    if due_at == issue.due_at:
        return False
    if owner != actor:
        raise Forbidden("not_owner", "Only its owner can change its due date")
    if due_at <= utc_now():
        raise Invalid("due_in_past", "The due date must be later than now")
    return True


def _note(note: str | None) -> str:
    text_note = (note or "").strip()
    if not text_note:
        raise Invalid("note_required", "Say how it was resolved: the rep tells the shop")
    if len(text_note) > limits.RESOLUTION_NOTE_MAX_CHARS:
        raise Invalid(
            "note_too_long", f"The note is {limits.RESOLUTION_NOTE_MAX_CHARS} characters at most"
        )
    return text_note


def _lock_issue(session: Session, org_id: str, issue_id: str) -> Issue:
    """SELECT … FOR UPDATE on one issue of this org, read fresh (R6). Another org's is 404."""
    return rows.find_one(session, Issue, org_id, issue_id, missing_issue(), for_update=True)
