"""A summary's narrative (batch 15b; BRIEF D16): Muse Spark writes the words, from the numbers
and quotes code picks, and code checks every sentence (CODING_GUIDELINES §9.6).

What it's given: the top NARRATIVE_ISSUES issues (a team's) or causes (the org's), ranked, of
those with a quote from the period, each with its numbers and up to NARRATIVE_QUOTES quotes: the
period's shown insights, the highest severity first, then the most recent, one per shop. One
with no quote from the period isn't sent: there'd be nothing to cite (15b-6, from the paid run).
Reps are only counts: no name is sent, so none can be ranked or named.

What it returns, in a strict schema: an overview, and sentences for each issue, every sentence
citing insights from those it was given (an enum of their IDs).

What's kept: a sentence citing at least one insight, every one of which is in the scope and
period and still shown (read fresh), with no number that isn't one of the summary's numbers;
the first OVERVIEW_SENTENCES of the overview and ISSUE_SENTENCES of an issue. The rest is
dropped, and counted by why (batch 15c's drop rate). With none left, the summary is only its
numbers.
"""

import json
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, TypedDict

from sqlalchemy import distinct, exists, select
from sqlalchemy.orm import Session

from countertalk import issues, limits, summaries
from countertalk.llm.base import Message
from countertalk.models import Evidence, Insight, InsightRoute, Team, Visit, parse_id
from countertalk.playbooks import OrgPack
from countertalk.roles import SHOPKEEPER
from countertalk.summaries import CauseNumbers, IssueNumbers, OrgNumbers, Period, TeamNumbers

NAME = "summary_narrative"
# Why a sentence was dropped, in the order they're checked.
DROPPED = ("extra", "uncited", "citation", "number")


class Citation(TypedDict):
    """An insight a sentence cites, and its visit, for the link."""

    insight_id: str
    visit_id: str


class Sentence(TypedDict):
    text: str
    cites: list[Citation]


class IssueWords(TypedDict):
    """An issue's sentences: `id` is its issue_id (a team's) or issue_key (the org's)."""

    id: str
    sentences: list[Sentence]


class Narrative(TypedDict):
    """What the checker kept, and how many sentences it dropped, by why (DROPPED)."""

    overview: list[Sentence]
    issues: list[IssueWords]
    kept: int
    dropped: dict[str, int]


PROMPT = """You write the words of a short business summary for an FMCG company's team, from \
numbers and quotes that code has already counted and picked. The numbers are right: never \
work out new ones.

Write in plain English, for busy readers:
- "overview": up to 3 sentences on the period as a whole.
- "issues": for each issue you're given, by its "id", 1 or 2 sentences on what shopkeepers \
said and what the numbers show.

Every sentence must:
- cite, in "cites", the IDs of the insights whose quotes support it (at least one);
- use only numbers that appear in the numbers given, copied exactly, as digits;
- leave out dates, percentages and money amounts, unless they're among the numbers given.

You're given only the issues with quotes from this period: others, with no quote, may rank \
higher, so don't call any issue you're given the biggest or the main one.

Don't name or rank anyone: reps are only counts. Don't guess at causes the quotes don't show."""


@dataclass(frozen=True)
class Quote:
    insight_id: str
    shop_id: str
    seen_at: datetime
    severity: str
    summary_en: str
    text: str


@dataclass(frozen=True)
class Section:
    """What a section's sentences may cite and say: an issue's own quotes' insights and its own
    numbers (and any in its title); the overview, any of them, and the totals."""

    cites: frozenset[str]
    numbers: frozenset[float]


@dataclass(frozen=True)
class Request:
    messages: list[Message]
    schema: dict[str, Any]
    # The insights offered: none means there's nothing to cite, so nothing is asked.
    offered: tuple[str, ...]
    overview: Section
    # By the ID the answer names an issue (or cause) with.
    sections: dict[str, Section]


def request_for(
    session: Session, pack: OrgPack, org_id: str, numbers: TeamNumbers | OrgNumbers
) -> Request:
    """The request for this summary's narrative (the module docstring)."""
    ranked = _items(numbers)
    # Only issues with a quote from the period: one with none has nothing to cite, so any
    # sentence about it would be dropped. Its numbers still show, with no words.
    quoted = _quoted(session, org_id, ranked, numbers.period)
    items = [(item_id, item) for item_id, item in ranked if item_id in quoted]
    items = items[: limits.NARRATIVE_ISSUES]
    quotes = _quotes(session, pack, org_id, items, numbers.period)
    described = [_described(item_id, item, quotes[item_id]) for item_id, item in items]
    sections = {
        found["id"]: Section(
            frozenset(q["insight_id"] for q in found["quotes"]),
            frozenset(_numbers_in_json(found["numbers"]))
            | frozenset(
                numbers_in(" ".join([found["title"], *(t["team"] for t in found.get("teams", []))]))
            ),
        )
        for found in described
    }
    offered = tuple(q["insight_id"] for found in described for q in found["quotes"])
    overview = Section(
        frozenset(offered),
        frozenset(_numbers_in_json(asdict(numbers.totals))).union(
            *(s.numbers for s in sections.values())
        ),
    )
    team = None
    if isinstance(numbers, TeamNumbers):
        team = session.scalars(select(Team.name).where(Team.id == numbers.team_id)).one()
    payload = {
        "for": f"the {team} team" if team else "sales leadership, across teams",
        "period": numbers.period.kind,
        "totals": asdict(numbers.totals),
        "issues": described,
    }
    messages = [
        Message("system", PROMPT),
        Message("user", json.dumps(payload, ensure_ascii=False, indent=1)),
    ]
    schema = _schema(list(sections), offered)
    return Request(messages, schema, offered, overview, sections)


def checked(
    session: Session,
    org_id: str,
    numbers: TeamNumbers | OrgNumbers,
    request: Request,
    answer: dict[str, Any],
) -> Narrative:
    """The narrative as kept: each section's sentences, each citation once with its visit for
    the link, and how many sentences were kept and dropped, by why."""
    team_id = numbers.team_id if isinstance(numbers, TeamNumbers) else None
    sentences = [*answer["overview"], *(s for i in answer["issues"] for s in i["sentences"])]
    cited = {i for s in sentences for i in s["cites"] if parse_id(i) is not None}
    citable = _citable(session, org_id, team_id, numbers.period, cited)
    dropped = dict.fromkeys(DROPPED, 0)

    def keep(found: Sequence[dict[str, Any]], section: Section | None) -> list[Sentence]:
        room = limits.OVERVIEW_SENTENCES if section is request.overview else limits.ISSUE_SENTENCES
        kept: list[Sentence] = []
        for sentence in found:
            why = "extra"
            if section is not None and len(kept) < room:
                why = _why_dropped(sentence, citable, section)
            if why is None:
                cites = [
                    Citation(insight_id=i, visit_id=citable[i])
                    for i in dict.fromkeys(sentence["cites"])
                ]
                kept.append(Sentence(text=sentence["text"].strip(), cites=cites))
            else:
                dropped[why] += 1
        return kept

    overview = keep(answer["overview"], request.overview)
    sections: list[IssueWords] = []
    seen = set()
    for issue in answer["issues"]:
        section = None if issue["id"] in seen else request.sections.get(issue["id"])
        seen.add(issue["id"])
        kept = keep(issue["sentences"], section)
        if kept:
            sections.append(IssueWords(id=issue["id"], sentences=kept))
    total = len(overview) + sum(len(s["sentences"]) for s in sections)
    return Narrative(overview=overview, issues=sections, kept=total, dropped=dropped)


# --- what it's given -------------------------------------------------------------------------


def _items(numbers: TeamNumbers | OrgNumbers) -> list[tuple[str, IssueNumbers | CauseNumbers]]:
    """The issues or causes, ranked, by the ID the answer names them with."""
    if isinstance(numbers, TeamNumbers):
        return [(n.issue_id, n) for n in numbers.issues]
    return [(c.issue_key, c) for c in numbers.causes]


def _issue_ids(item: IssueNumbers | CauseNumbers) -> list[str]:
    if isinstance(item, IssueNumbers):
        return [item.issue_id]
    return [work.issue_id for work in item.teams]


def _owners(items: list[tuple[str, IssueNumbers | CauseNumbers]]) -> dict[str, str]:
    """Each issue ID, to the item (an issue, or a cause of several) it's part of."""
    return {issue_id: item_id for item_id, item in items for issue_id in _issue_ids(item)}


def _quotable(org_id: str, period: Period, issue_ids: Iterable[str], *columns: Any) -> Any:
    """These issues' quotes from the period: the shopkeeper's words of shown insights."""
    return (
        select(*columns)
        .select_from(InsightRoute)
        .join(Insight, Insight.id == InsightRoute.insight_id)
        .join(Visit, Visit.id == Insight.visit_id)
        .join(Evidence, Evidence.insight_id == Insight.id)
        .where(
            InsightRoute.issue_id.in_(list(issue_ids)),
            *summaries.shown_in(org_id, period),
            Evidence.speaker_role == SHOPKEEPER,
            Evidence.quote_final.is_not(None),
        )
    )


def _quoted(
    session: Session,
    org_id: str,
    items: list[tuple[str, IssueNumbers | CauseNumbers]],
    period: Period,
) -> set[str]:
    """The items with a quote from the period: one row an issue, not every quote."""
    owner = _owners(items)
    found = _quotable(org_id, period, owner, distinct(InsightRoute.issue_id))
    return {owner[issue_id] for issue_id in session.scalars(found)}


def _quotes(
    session: Session,
    pack: OrgPack,
    org_id: str,
    items: list[tuple[str, IssueNumbers | CauseNumbers]],
    period: Period,
) -> dict[str, list[Quote]]:
    """Each item's quotes (the module docstring): the shopkeeper's words of its first evidence."""
    owner = _owners(items)
    columns = (InsightRoute.issue_id, Insight.id, Visit.shop_id, Visit.created_at)
    quoted = (Insight.severity, Insight.summary_en, Evidence.quote_final)
    rows = session.execute(
        _quotable(org_id, period, owner, *columns, *quoted).order_by(Evidence.seq)
    )
    found: dict[str, dict[str, Quote]] = {item_id: {} for item_id, _ in items}
    for issue_id, insight_id, *quote in rows:
        found[owner[issue_id]].setdefault(insight_id, Quote(insight_id, *quote))
    picked: dict[str, list[Quote]] = {}
    for item_id, by_insight in found.items():
        ranked = sorted(
            by_insight.values(),
            key=lambda q: (issues.severity_rank(pack, q.severity), -q.seen_at.timestamp()),
        )
        one_per_shop = {}
        for quote in ranked:
            one_per_shop.setdefault(quote.shop_id, quote)
        picked[item_id] = list(one_per_shop.values())[: limits.NARRATIVE_QUOTES]
    return picked


def _described(
    item_id: str, item: IssueNumbers | CauseNumbers, quotes: list[Quote]
) -> dict[str, Any]:
    m = item.measures
    found: dict[str, Any] = {
        "id": item_id,
        "title": item.title_en,
        "category": item.category,
    }
    if isinstance(item, IssueNumbers):
        found["status"] = item.status
    else:
        found["teams"] = [{"team": w.team_name, "status": w.status} for w in item.teams]
    found["numbers"] = {
        "trend": m.label,
        "severity": m.severity,
        "shops": m.shops,
        "insights": m.insights,
        "reps": m.reps,
        "visits": m.visits,
        "shops_raising_it_again": m.raised_again,
        "open_shops": m.open_shops,
        "age_in_weeks": m.age_weeks,
        "overdue": m.overdue,
        "usual_shops_per_period": m.baseline,
        "rank_score": m.score,
    }
    found["quotes"] = [
        {"insight_id": q.insight_id, "shopkeeper_said": q.text, "in_english": q.summary_en}
        for q in quotes
    ]
    return found


def _schema(item_ids: list[str], offered: tuple[str, ...]) -> dict[str, Any]:
    sentence = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "cites": {"type": "array", "items": {"type": "string", "enum": list(offered)}},
        },
        "required": ["text", "cites"],
        "additionalProperties": False,
    }
    issue = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "enum": item_ids},
            "sentences": {"type": "array", "items": {"$ref": "#/$defs/sentence"}},
        },
        "required": ["id", "sentences"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "overview": {"type": "array", "items": {"$ref": "#/$defs/sentence"}},
            "issues": {"type": "array", "items": issue},
        },
        "required": ["overview", "issues"],
        "additionalProperties": False,
        "$defs": {"sentence": sentence},
    }


# --- the checker -----------------------------------------------------------------------------

# Digits, with thousands commas (4,500) and decimals, but not ones in a name (7UP, 250ml, S01);
# and number words, which a sentence can use to slip a number past the digits ("one" is left
# out: "one of them" says no count).
DIGITS = re.compile(r"(?<![\dA-Za-z])\d[\d,]*(?:\.\d+)?(?![\dA-Za-z])")
WORDS: dict[str, float] = {
    **dict(
        zip(
            "two three four five six seven eight nine ten eleven twelve thirteen fourteen"
            " fifteen sixteen seventeen eighteen nineteen twenty".split(),
            range(2, 21),
            strict=True,
        )
    ),
    **{
        tens: 10 * n
        for n, tens in enumerate("thirty forty fifty sixty seventy eighty ninety".split(), 3)
    },
    "hundred": 100,
    "thousand": 1000,
    "dozen": 12,
    "twice": 2,
    "half": 0.5,
}


def numbers_in(text: str) -> list[float]:
    found = [float(m.rstrip(",").replace(",", "")) for m in DIGITS.findall(text)]
    found += [float(WORDS[w]) for w in re.findall(r"[a-z]+", text.lower()) if w in WORDS]
    return found


def _numbers_in_json(value: Any) -> Iterator[float]:
    if isinstance(value, bool):
        return
    if isinstance(value, int | float):
        yield float(value)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _numbers_in_json(item)
    elif isinstance(value, list):
        for item in value:
            yield from _numbers_in_json(item)


def _why_dropped(sentence: dict[str, Any], citable: dict[str, str], section: Section) -> str | None:
    if not sentence["text"].strip() or not sentence["cites"]:
        return "uncited"
    if any(i not in citable or i not in section.cites for i in sentence["cites"]):
        return "citation"
    if any(n not in section.numbers for n in numbers_in(sentence["text"])):
        return "number"
    return None


def _citable(
    session: Session, org_id: str, team_id: str | None, period: Period, cited: set[str]
) -> dict[str, str]:
    """Of the cited insights, the ones in the scope and period and still shown: their visits."""
    query = (
        select(Insight.id, Insight.visit_id)
        .join(Visit, Visit.id == Insight.visit_id)
        .where(Insight.id.in_(cited), *summaries.shown_in(org_id, period))
    )
    if team_id is not None:
        query = query.where(
            exists().where(InsightRoute.insight_id == Insight.id, InsightRoute.team_id == team_id)
        )
    return {insight_id: visit_id for insight_id, visit_id in session.execute(query)}
