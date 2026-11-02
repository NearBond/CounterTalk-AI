"""Merge suggestions (batch 15c; BRIEF D16): the AI suggests, a person decides.

When: in a team summary's job, before its narrative, once (the summary's merge_step), for each
category with two or more of the team's active issues. A retried job doesn't ask again.

What Muse Spark sees: each issue's title (its category and key labels) and up to
MERGE_INSIGHT_SUMMARIES of its newest insights' English summaries. It answers in a strict schema
whose IDs are the issues it was given: [{into, merge: [ids], reason}], or nothing. A category
the org's playbook no longer has is skipped.

What code keeps (§9.6), a suggestion at a time, or none of it: it merges something; every ID
was given; no ID appears twice in what's left, so nothing chains; its reason, shown to the
team, isn't blank or over MERGE_REASON_MAX_CHARS; and, read fresh and locked as it's saved,
both issues are still
active, the team's, and of the category. A pair already suggested is never stored again, either
way round: one dismissed is never suggested again. Each kept pair is an IssueMergeSuggestion,
open until a person merges (issues.merge) or dismisses it.

A call that fails is counted and skipped, and the summary job keeps any other failure of the
step from failing the summary: suggestions are an extra, never a reason to fail it or to pay
again for its narrative. Only a lost claim stops it. A merge marks its pair's suggestion
merged (issues.merge). The words of the request and answer are never logged;
only counts are.
"""

import json
import logging
import threading
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased

from countertalk import issues, limits, llm_calls, rows
from countertalk.db import session_scope
from countertalk.errors import Conflict, NotFound
from countertalk.llm.base import Message, StructuredLlm
from countertalk.models import (
    ACTIVE_ISSUE,
    Insight,
    InsightRoute,
    Issue,
    IssueMergeSuggestion,
    LlmPurpose,
    SuggestionStatus,
    Visit,
    utc_now,
)
from countertalk.playbooks import OrgPack

log = logging.getLogger(__name__)

NAME = "merge_suggestions"
# Why a suggestion was dropped, in the order they're checked.
DROPPED = ("empty", "outside", "twice", "reason", "closed", "already")

PROMPT = """You help an FMCG company's team keep its list of issues tidy. Each issue groups \
shopkeepers' findings that share a likely cause. Sometimes one cause ends up as two issues: \
a scheme or product the finding didn't name, or a distributor spelled another way.

You're given one team's open issues in one category, each with its title and up to 3 of its \
findings. Suggest merging issues only when they are clearly the same problem for the same \
shops' supplier, scheme or product. If in doubt, suggest nothing: a person decides each one.

For each suggestion: "into" is the issue to keep (the one whose title names the cause best), \
"merge" the issues that are the same as it, and "reason" one plain sentence for the team, with \
no names of people. Use each issue at most once. Return an empty list when nothing should merge."""


@dataclass(frozen=True)
class Ask:
    """One category's request: its issues' IDs, and the messages and schema."""

    category: str
    issue_ids: tuple[str, ...]
    messages: list[Message]
    schema: dict[str, Any]


def asks_for(session: Session, pack: OrgPack, org_id: str, team_id: str) -> list[Ask]:
    """A request for each category with two or more of the team's active issues with routes."""
    found = session.scalars(
        select(Issue)
        .where(
            Issue.org_id == org_id,
            Issue.team_id == team_id,
            issues.active(),
            issues.with_routes(),
        )
        .order_by(Issue.category, Issue.seq)
    ).all()
    by_category: dict[str, list[Issue]] = defaultdict(list)
    for issue in found:
        by_category[issue.category].append(issue)
    # Two or more, of a category the playbook still has (its name goes in the request).
    book = pack.playbook.categories
    wanted = {c: grouped for c, grouped in by_category.items() if len(grouped) >= 2 and c in book}
    said = _newest_summaries(session, [i.id for grouped in wanted.values() for i in grouped])
    asks = []
    for category, grouped in wanted.items():
        ids = tuple(issue.id for issue in grouped)
        payload = {
            "category": book[category].name,
            "issues": [{"id": i.id, "title": i.title_en, "findings": said[i.id]} for i in grouped],
        }
        messages = [
            Message("system", PROMPT),
            Message("user", json.dumps(payload, ensure_ascii=False, indent=1)),
        ]
        asks.append(Ask(category, ids, messages, _schema(list(ids))))
    return asks


def suggest(
    summary_id: str,
    llm: StructuredLlm,
    claim_lost: threading.Event | None,
    pack: OrgPack,
    org_id: str,
    team_id: str,
) -> dict[str, Any]:
    """The step (the module docstring), with no transaction open while it asks: what it did,
    for the summary's merge_step."""
    with session_scope(read_only=True) as session:
        asks = asks_for(session, pack, org_id, team_id)
    done: Counter[str] = Counter()
    dropped = dict.fromkeys(DROPPED, 0)
    pairs: list[tuple[str, str, str]] = []
    for ask in asks:
        outcome = llm.complete(ask.messages, NAME, ask.schema, claim_lost=claim_lost)
        llm_calls.record(None, LlmPurpose.MERGE, llm.provider, outcome, summary_id=summary_id)
        done["asked"] += 1
        if outcome.error is not None:
            if outcome.error.code == "claim_lost":
                raise outcome.error
            done["failed"] += 1  # an extra: skipped, never retried
            continue
        assert outcome.answer is not None
        kept, why = _checked(outcome.answer["suggestions"], set(ask.issue_ids))
        pairs += kept
        for reason, count in why.items():
            dropped[reason] += count
    with session_scope() as session:
        stored, why = _store(session, org_id, team_id, summary_id, pairs)
    for reason, count in why.items():
        dropped[reason] += count
    step = {"asked": done["asked"], "failed": done["failed"], "kept": stored, "dropped": dropped}
    log.info(
        "merge suggestions summary=%s asked=%d failed=%d kept=%d %s",
        summary_id,
        step["asked"],
        step["failed"],
        stored,
        " ".join(f"dropped_{why}={n}" for why, n in dropped.items()),
    )
    return step


@dataclass(frozen=True)
class Open:
    """An open suggestion as one of its issues shows it: the other issue, as it is now."""

    suggestion: IssueMergeSuggestion
    other: Issue


def open_for(session: Session, org_id: str, issue_id: str) -> list[Open]:
    """The issue's open suggestions whose two issues are both still active with findings: one
    merged or closed some other way since isn't offered."""
    into, merged = aliased(Issue), aliased(Issue)
    found = session.execute(
        select(IssueMergeSuggestion, into, merged)
        .join(into, into.id == IssueMergeSuggestion.into_issue_id)
        .join(merged, merged.id == IssueMergeSuggestion.merge_issue_id)
        .where(
            IssueMergeSuggestion.org_id == org_id,
            IssueMergeSuggestion.status == SuggestionStatus.OPEN,
            (IssueMergeSuggestion.into_issue_id == issue_id)
            | (IssueMergeSuggestion.merge_issue_id == issue_id),
            *(issues.active(i) for i in (into, merged)),
            *(issues.with_routes(i) for i in (into, merged)),
        )
        .order_by(IssueMergeSuggestion.created_at, IssueMergeSuggestion.id)
    ).all()
    return [
        Open(s, merged_issue if s.into_issue_id == issue_id else into_issue)
        for s, into_issue, merged_issue in found
    ]


def accept(
    session: Session,
    pack_for: Callable[[], OrgPack],
    org_id: str,
    actor: str,
    suggestion_id: str,
    versions: tuple[int, int],
) -> Issue:
    """A person merges the pair as suggested: `versions` are the two issues' as read, the one to
    keep first. The suggestion is locked first, so a Dismiss can't land between (the lock order:
    the suggestion, then issues.merge's). Merged already: nothing changes. Dismissed: 409.
    Returns the issue kept."""
    found = rows.find_one(
        session, IssueMergeSuggestion, org_id, suggestion_id, _missing(), for_update=True
    )
    if found.status == SuggestionStatus.DISMISSED:
        raise Conflict("suggestion_dismissed", "It was dismissed: they stay apart")
    into_version, merge_version = versions
    return issues.merge(
        session,
        pack_for,
        org_id,
        actor,
        (found.into_issue_id, into_version),
        (found.merge_issue_id, merge_version),
    )


def dismiss(session: Session, org_id: str, actor: str, suggestion_id: str) -> IssueMergeSuggestion:
    """A person says the pair isn't one issue: it's never suggested again. Dismissing it again
    changes nothing; one merged already is 409."""
    found = rows.find_one(
        session, IssueMergeSuggestion, org_id, suggestion_id, _missing(), for_update=True
    )
    if found.status == SuggestionStatus.MERGED:
        raise Conflict("suggestion_merged", "It was merged already")
    if found.status == SuggestionStatus.OPEN:
        found.status = SuggestionStatus.DISMISSED.value
        found.decided_by_id, found.decided_at = actor, utc_now()
        log.info("merge suggestion dismissed suggestion=%s", found.id)
    return found


def _missing() -> NotFound:
    """A fresh one each time (errors_are_raised_fresh)."""
    return NotFound("suggestion_not_found", "There's no such suggestion")


def _checked(
    answer: list[dict[str, Any]], given: set[str]
) -> tuple[list[tuple[str, str, str]], Counter[str]]:
    """The kept pairs (into, merged, reason), and the suggestions dropped, by why."""
    dropped = Counter[str]()
    named = []
    for suggestion in answer:
        ids = [suggestion["into"], *suggestion["merge"]]
        if not suggestion["merge"]:
            dropped["empty"] += 1
        elif any(i not in given for i in ids):
            dropped["outside"] += 1
        else:
            named.append((suggestion, ids))
    seen = Counter(i for _, ids in named for i in ids)
    kept = []
    for suggestion, ids in named:
        reason = suggestion["reason"].strip()
        if any(seen[i] > 1 for i in ids):
            dropped["twice"] += 1
        elif not reason or len(reason) > limits.MERGE_REASON_MAX_CHARS:
            dropped["reason"] += 1  # shown to the team: never blank, never a page
        else:
            kept += [(suggestion["into"], merged, reason) for merged in suggestion["merge"]]
    return kept, dropped


def _store(
    session: Session,
    org_id: str,
    team_id: str,
    summary_id: str,
    pairs: list[tuple[str, str, str]],
) -> tuple[int, Counter[str]]:
    """Each pair, read fresh: both still active, the team's and of one category; and not
    suggested before, either way round. How many were stored, and the rest by why."""
    ids = {i for into, merged, _ in pairs for i in (into, merged)}
    # Locked in §2.3's order and read fresh (R6): a merge or status change can't slip in
    # between this check and the insert.
    fresh = {
        issue.id: issue
        for issue in session.scalars(
            select(Issue)
            .where(Issue.org_id == org_id, Issue.id.in_(ids))
            .order_by(Issue.team_id, Issue.issue_key, Issue.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    dropped, stored = Counter[str](), 0
    for into, merged, reason in pairs:
        if not _mergeable(fresh.get(into), fresh.get(merged), team_id):
            dropped["closed"] += 1
            continue
        made = session.scalar(
            insert(IssueMergeSuggestion)
            .values(
                org_id=org_id,
                team_id=team_id,
                into_issue_id=into,
                merge_issue_id=merged,
                pair_key=issues.pair_key(into, merged),
                reason=reason,
                status=SuggestionStatus.OPEN.value,
                summary_id=summary_id,
            )
            .on_conflict_do_nothing(index_elements=["org_id", "pair_key"])
            .returning(IssueMergeSuggestion.id)
        )
        if made is None:
            dropped["already"] += 1  # suggested before, dismissed or not
        else:
            stored += 1
    return stored, dropped


def _mergeable(into: Issue | None, merged: Issue | None, team_id: str) -> bool:
    """Both still there and active, the team's, and of one category."""
    if into is None or merged is None:
        return False
    both = (into, merged)
    return (
        all(i.status in ACTIVE_ISSUE and i.team_id == team_id for i in both)
        and into.category == merged.category
    )


def _newest_summaries(session: Session, issue_ids: list[str]) -> dict[str, list[str]]:
    """Each issue's newest shown insights' English summaries, MERGE_INSIGHT_SUMMARIES at most."""
    newest = func.row_number().over(
        partition_by=InsightRoute.issue_id, order_by=(Visit.created_at.desc(), Insight.id)
    )
    ranked = (
        select(InsightRoute.issue_id, Insight.summary_en, newest.label("n"))
        .join(Insight, Insight.id == InsightRoute.insight_id)
        .join(Visit, Visit.id == Insight.visit_id)
        .where(InsightRoute.issue_id.in_(issue_ids), Insight.visible.is_(True))
        .subquery()
    )
    found: dict[str, list[str]] = defaultdict(list)
    for issue_id, summary_en in session.execute(
        select(ranked.c.issue_id, ranked.c.summary_en)
        .where(ranked.c.n <= limits.MERGE_INSIGHT_SUMMARIES)
        .order_by(ranked.c.issue_id, ranked.c.n)
    ):
        found[issue_id].append(summary_en)
    return found


def _schema(issue_ids: list[str]) -> dict[str, Any]:
    one_of_them = {"type": "string", "enum": issue_ids}
    suggestion = {
        "type": "object",
        "properties": {
            "into": one_of_them,
            "merge": {"type": "array", "items": one_of_them},
            "reason": {"type": "string"},
        },
        "required": ["into", "merge", "reason"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"suggestions": {"type": "array", "items": suggestion}},
        "required": ["suggestions"],
        "additionalProperties": False,
    }
