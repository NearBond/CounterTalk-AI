"""K6, the planted patterns (BUILD_PLAN K6): world.yaml loaded into a new org of its own, the
app's way and in the world's own time, for 15b's tests and 15c's `run_eval --patterns`.

Each visit is built as extraction leaves one (a transcript of the shopkeeper's one line with
its saved role, an extraction, a verified insight quoting it, routed by the rule), then
confirmed by its rep: issues are filed, joined and reopened by issues.py itself. The clock
issues.py reads is the world's at each event (a visit at 11:00 IST on its day, a resolve at
15:00), so due dates, resolves and reopens fall in the world's weeks. No audio: the quotes are
text (batch 20 adds clips when it seeds the demo org).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

import yaml
from sqlalchemy import select

from countertalk import catalog, consent, issues, review, routing
from countertalk.config import BACKEND_DIR
from countertalk.db import session_scope
from countertalk.models import (
    IST,
    CatalogItem,
    Distributor,
    Evidence,
    EvidenceMatch,
    Extraction,
    Insight,
    InsightRoute,
    Issue,
    Org,
    Role,
    Shop,
    SpeakerRole,
    User,
    Visit,
    VisitClient,
    VisitKind,
    VisitStatus,
    new_id,
)
from countertalk.playbooks import ORG_PACKS_DIR, PLAYBOOKS_DIR, OrgPack, load_org_pack
from countertalk.roles import SHOPKEEPER
from countertalk.summaries import Period, period_of
from countertalk.transcription import add_transcript

WORLD = BACKEND_DIR / "tests" / "fixtures" / "patterns" / "world.yaml"
EXPECTED = BACKEND_DIR / "tests" / "fixtures" / "patterns" / "expected.yaml"
VISIT_AT = time(11)
RESOLVE_AT = time(15)
LABEL = "c0:B"


@dataclass
class World:
    org_id: str
    pack: OrgPack
    w1_start: date
    teams: dict[str, str]
    # The finance team member, who merges and resolves: finance_email(org_id) is theirs.
    finance: str
    # S01-S30 and K6's letters, to shop IDs.
    shops: dict[str, str]
    # P1-P5 to their insights' IDs, in the world's order.
    insights: dict[str, list[str]] = field(default_factory=dict)
    # The catalog's keys to item IDs: a key the fixture spells wrong fails, never links to none.
    items: dict[str, str] = field(default_factory=dict)

    def week(self, number: int) -> Period:
        return period_of("week", self.w1_start + timedelta(weeks=number - 1))


def finance_email(org_id: str) -> str:
    """The finance member's example address, to sign in as (scripts/set_password.py)."""
    return f"finance-{org_id[:8]}@example.com"


def load(
    path: Path = WORLD,
    packs: Path = ORG_PACKS_DIR,
    playbooks: Path = PLAYBOOKS_DIR,
    w1_start: date | None = None,
) -> World:
    """The world, from its own W1 (21 Sep 2026), or from `w1_start` (a Monday): the click check
    (batch 15c) moves it so its W6 is always last week, as the summaries page shows it."""
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    if w1_start is not None:
        if w1_start.weekday() != 0:
            raise ValueError("the world starts on a Monday")
        spec["w1_start"] = w1_start
    pack = load_org_pack(spec["org_pack"]["id"], spec["org_pack"]["version"], packs, playbooks)
    world, people = _org(spec, pack)
    events = [("visit", _moment(spec, v["week"], v["day"], VISIT_AT), v) for v in spec["visits"]]
    events += [
        ("resolve", _moment(spec, r["week"], r["day"], RESOLVE_AT), r) for r in spec["resolves"]
    ]
    for kind, moment, event in sorted(events, key=lambda e: e[1]):
        with _clock(moment):
            if kind == "visit":
                _visit(world, people, event, moment)
            else:
                _resolve(world, world.finance, event)
    return world


def expected() -> dict[str, Any]:
    return yaml.safe_load(EXPECTED.read_text(encoding="utf-8"))


@contextmanager
def _clock(moment: datetime) -> Iterator[None]:
    """issues.py's clock, at the world's moment."""
    with mock.patch.object(issues, "utc_now", lambda: moment):
        yield


def _moment(spec: dict[str, Any], week: str, day: int, at: time) -> datetime:
    start = spec["w1_start"] + timedelta(weeks=int(week.removeprefix("W")) - 1, days=day)
    return datetime.combine(start, at, IST)


def _org(spec: dict[str, Any], pack: OrgPack) -> tuple[World, dict[str, str]]:
    with session_scope() as session:
        org = Org(name=f"K6 world (example) {new_id()[:8]}")
        session.add(org)
        session.flush()
        catalog.import_org_pack(session, org.id, pack)
        people = {}
        for number, rep in enumerate(spec["reps"], start=1):
            user = User(
                org_id=org.id,
                email=f"{rep}-{org.id[:8]}@example.com",
                name=f"Rep {number} (example)",
                role=Role.REP,
            )
            session.add(user)
            session.flush()
            people[rep] = user.id
        member = User(
            org_id=org.id,
            email=finance_email(org.id),
            name="Finance member (example)",
            role=Role.TEAM_MEMBER,
            team_id=routing.team_ids(session, org.id)["finance"],
        )
        session.add(member)
        session.flush()
        distributors = {
            key: session.scalars(
                select(Distributor).where(Distributor.org_id == org.id, Distributor.key == key)
            ).one()
            for key in spec["distributors"].values()
        }
        shops = {}
        for name, letter_key in spec["shops"].items():
            found = distributors[spec["distributors"][letter_key]]
            shop = Shop(
                org_id=org.id,
                name=f"Shop {name.removeprefix('S')} (example)",
                area=found.area,
                distributor_id=found.id,
            )
            session.add(shop)
            session.flush()
            shops[name] = shop.id
        for letter, name in spec["letters"].items():
            shops[letter] = shops[name]
        # Batch 17: its shops agreed on paper, like every shop in the data (the user, 3 Nov).
        consent.mark_unanswered(session, org.id, next(iter(people.values())), "hi")
        teams = routing.team_ids(session, org.id)
        items = {
            key: item_id
            for key, item_id in session.execute(
                select(CatalogItem.key, CatalogItem.id).where(CatalogItem.org_id == org.id)
            )
        }
        world = World(org.id, pack, spec["w1_start"], teams, member.id, shops)
        world.items = items
        return world, people


def _visit(world: World, people: dict[str, str], event: dict[str, Any], moment: datetime) -> None:
    with session_scope() as session:
        visit = Visit(
            org_id=world.org_id,
            shop_id=world.shops[event["shop"]],
            rep_id=people[event["rep"]],
            kind=VisitKind.CONVERSATION,
            client=VisitClient.UPLOAD,
            status=VisitStatus.REVIEW,
            source_ext="m4a",
            duration_ms=5_000,
            completed_at=moment,
            created_at=moment,
        )
        session.add(visit)
        session.flush()
        quote = event["quote"]
        turn = {"turn_id": 0, "speaker_label": LABEL, "start_ms": 0, "end_ms": 5_000, "text": quote}
        transcript_id = add_transcript(session, visit, "k6", "k6-world", "hi,en", [turn])
        book = world.pack.playbook
        session.add(
            SpeakerRole(
                transcript_id=transcript_id,
                speaker_label=LABEL,
                role=SHOPKEEPER,
                confidence=1.0,
                playbook_id=book.id,
                playbook_version=book.version,
            )
        )
        extraction = Extraction(
            id=new_id(),
            visit_id=visit.id,
            transcript_id=transcript_id,
            playbook_id=book.id,
            playbook_version=book.version,
            org_pack_id=world.pack.id,
            org_pack_version=world.pack.version,
            model="k6-world",
            prompt_hash="k6",
            raw_json={},
        )
        session.add(extraction)
        session.flush()
        insight = Insight(
            id=new_id(),
            org_id=world.org_id,
            visit_id=visit.id,
            seq=0,
            extraction_id=extraction.id,
            kind=event["kind"],
            category=event["category"],
            severity=event["severity"],
            summary_en=event["summary"],
            sku_mentions=[],
            competitor_mentions=[],
            sku_id=world.items[event["sku"]] if "sku" in event else None,
            competitor_id=world.items[event["competitor"]] if "competitor" in event else None,
            scheme_key=event.get("scheme"),
            verified=True,
        )
        session.add(insight)
        session.flush()
        session.add(
            Evidence(
                insight_id=insight.id,
                visit_id=visit.id,
                seq=0,
                transcript_id=transcript_id,
                turn_id=0,
                speaker_role=SHOPKEEPER,
                quote_llm=quote,
                quote_final=quote,
                match=EvidenceMatch.EXACT,
                char_start=0,
                char_end=len(quote),
                start_ms=0,
            )
        )
        decision = routing.decide(
            event["category"], event["severity"], None, routing.table_of(world.pack), world.teams
        )
        routing.route_new(session, insight, decision, world.teams)
        world.insights.setdefault(event["pattern"], []).append(insight.id)
        visit_id, rep_id = visit.id, visit.rep_id
    with session_scope() as session:
        review.confirm(session, lambda: world.pack, world.org_id, rep_id, visit_id)


def _resolve(world: World, actor: str, event: dict[str, Any]) -> None:
    """The pattern's first insight's issue in that team, resolved with the note."""
    with session_scope() as session:
        first = world.insights[event["pattern"]][0]
        issue = session.scalars(
            select(Issue)
            .join(InsightRoute, InsightRoute.issue_id == Issue.id)
            .where(InsightRoute.insight_id == first, Issue.team_id == world.teams[event["team"]])
        ).one()
        issues.change(
            session,
            world.org_id,
            actor,
            issue.id,
            issue.version,
            status="resolved",
            note=event["note"],
        )


def issue_of(world: World, insight_id: str, team: str) -> str:
    """The issue an insight is in, in a team's inbox."""
    with session_scope(read_only=True) as session:
        return session.scalars(
            select(InsightRoute.issue_id).where(
                InsightRoute.insight_id == insight_id, InsightRoute.team_id == world.teams[team]
            )
        ).one()


def expected_merge(world: World) -> tuple[str, str]:
    """K6's one expected merge suggestion: E's issue (its scheme isn't linked) into P1's, both
    finance's. (P1's issue, E's issue)."""
    with session_scope(read_only=True) as session:
        e_insight = session.scalars(
            select(Insight.id)
            .join(Visit, Visit.id == Insight.visit_id)
            .where(Insight.id.in_(world.insights["P1"]), Visit.shop_id == world.shops["E"])
        ).one()
    return issue_of(world, world.insights["P1"][0], "finance"), issue_of(
        world, e_insight, "finance"
    )
