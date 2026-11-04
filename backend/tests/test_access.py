"""The §7.2 matrix (batch 16): every API route against every role, and two orgs.

Each case is one route with a valid request (so the access check is what answers, never the
body's validation) and what each role gets: "ok" (anything but a refusal: 200, 201, 204, or the
route's own 400 or 409 once past access), 404 with the refusal's code, or 403 with its code.
A route missing from the table fails test_every_route_is_in_the_matrix. Roles: the visits' rep,
another rep of the org, a member of the first team (finance), one of the second
(distribution), a manager and an admin. Each case gets a world of its own, and the roles a case
refuses go first, so an allowed write can't change what they'd see.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from countertalk import extraction, issues, review, routing, summary_jobs
from countertalk.db import session_scope
from countertalk.entities import link_key
from countertalk.main import create_app
from countertalk.models import (
    AliasReview,
    CatalogAlias,
    CatalogKind,
    Insight,
    InsightRoute,
    IssueMergeSuggestion,
    Role,
    Visit,
    VisitClient,
    utc_now,
)
from countertalk.playbooks import ORG_PACKS_DIR, OrgPack, load_org_pack
from scripts.seed_dev import DEMO_PACK
from tests.conftest import ORG_PACK_FIXTURES, a_user, sign_in, wipe_database
from tests.test_auth import PUBLIC, api_routes
from tests.test_extraction import answer, insight, transcribed
from tests.test_inbox_api import shop
from tests.test_narrative import at_shop
from tests.test_review_api import PAYMENT, seeded_demo
from tests.test_roles import ALL_PLACED, FakeLlm, tiny, tiny_org
from tests.test_summary_jobs import LAST_WEEK
from tests.test_visits import Org_, new_visit

ROLES = ("rep", "other_rep", "first", "second", "manager", "admin")
OK = "ok"
# What a refusal answers: 404 with one of these codes (never 403, which would say it exists), or
# 403 with one of FORBIDDEN's.
NOT_FOUND = {
    "not_found",
    "visit_not_found",
    "insight_not_found",
    "issue_not_found",
    "suggestion_not_found",
    "summary_not_found",
    "team_not_found",
    "shop_not_found",
}
FORBIDDEN = {"not_your_visit", "not_your_team", "not_a_manager"}


@dataclass
class World:
    org_id: str
    users: dict[str, str]
    teams: dict[str, str]  # "first", "second": team IDs
    keys: dict[str, str]  # "first", "second": team keys
    shop_id: str
    recording: str  # the rep's visit, still recording
    review: str  # the rep's visit in review, and its insight
    review_insight: str
    confirmed: str  # the rep's confirmed visit: its insight routed to the first team, its issue
    routed_insight: str
    issue: str
    triage_insight: str  # a confirmed insight with no team
    suggestion: str  # the first team's merge suggestion
    team_summary: str
    org_summary: str
    catalog_item: str  # one of the org's own items, an alias of it, and a name to review
    catalog_alias: str
    alias_review: str
    extra: dict[str, str] = field(default_factory=dict)


def build(org: Org_, pack: OrgPack, keys: tuple[str, str], second_shop: Org_, packs: Path) -> World:
    """The world of an org: its users, visits, insights, issues, a suggestion and summaries.
    `packs`: the folder its org pack is in."""
    first_key, second_key = keys
    users = {
        "rep": org.rep_id,
        "other_rep": a_user(org.org_id, Role.REP, "Other Rep"),
        "first": a_user(org.org_id, Role.TEAM_MEMBER, "First Member", first_key),
        "second": a_user(org.org_id, Role.TEAM_MEMBER, "Second Member", second_key),
        "manager": a_user(org.org_id, Role.MANAGER, "The Manager"),
        "admin": a_user(org.org_id, Role.ADMIN, "The Admin"),
    }
    with session_scope(read_only=True) as session:
        teams = routing.team_ids(session, org.org_id)
    review_visit = reviewed(org, packs, PAYMENT)
    confirmed = _confirmed(org, pack, packs, PAYMENT)
    other = _confirmed(second_shop, pack, packs, PAYMENT)  # the first team's second issue
    others_rep = Org_(org.org_id, users["other_rep"], org.shop_id)
    _confirmed(others_rep, pack, packs, insight(category="supply", suggested_team=second_key))
    triaged = _confirmed(org, pack, packs, insight(category="other", suggested_team=None))
    with session_scope() as session:
        [review_insight] = _insights(session, review_visit)
        [routed] = _insights(session, confirmed)
        [triage_insight] = _insights(session, triaged)
        issue = _issue_of(session, routed)
        [other_insight] = _insights(session, other)
        other_issue = _issue_of(session, other_insight)
        suggestion = IssueMergeSuggestion(
            org_id=org.org_id,
            team_id=teams[first_key],
            into_issue_id=issue,
            merge_issue_id=other_issue,
            pair_key=issues.pair_key(issue, other_issue),
            reason="The same payment.",
        )
        session.add(suggestion)
        session.flush()
        now = utc_now()
        team_summary = summary_jobs.ask(
            session, org.org_id, "team", teams[first_key], "week", now
        ).id
        org_summary = summary_jobs.ask(session, org.org_id, "org", None, "month", now).id
        suggestion_id = suggestion.id
        catalog_item, catalog_alias = session.execute(
            select(CatalogAlias.item_id, CatalogAlias.id)
            .where(CatalogAlias.org_id == org.org_id)
            .order_by(CatalogAlias.text)
            .limit(1)
        ).one()
        alias_review = AliasReview(
            org_id=org.org_id,
            kind=CatalogKind.COMPETITOR,
            text="Zorblax",
            normalised=link_key("Zorblax"),
            visit_id=confirmed,
        )
        session.add(alias_review)
        session.flush()
        alias_review_id = alias_review.id
    return World(
        org_id=org.org_id,
        users=users,
        teams={"first": teams[first_key], "second": teams[second_key]},
        keys={"first": first_key, "second": second_key},
        shop_id=org.shop_id,
        recording=new_visit(org, client=VisitClient.WEB),
        review=review_visit,
        review_insight=review_insight,
        confirmed=confirmed,
        routed_insight=routed,
        issue=issue,
        triage_insight=triage_insight,
        suggestion=suggestion_id,
        team_summary=team_summary,
        org_summary=org_summary,
        catalog_item=catalog_item,
        catalog_alias=catalog_alias,
        alias_review=alias_review_id,
    )


def reviewed(org: Org_, packs: Path, found: dict[str, Any]) -> str:
    """A visit of the org's rep, extracted (a fake) and in review: its ID."""
    visit_id = transcribed(org)
    extraction.extract(visit_id, FakeLlm(ALL_PLACED, answer(found)), packs=packs)
    return visit_id


def _confirmed(org: Org_, pack: OrgPack, packs: Path, found: dict[str, Any]) -> str:
    """A visit of the org's rep, dated last week and confirmed: its ID."""
    visit_id = reviewed(org, packs, found)
    with session_scope() as session:
        session.execute(
            update(Visit)
            .where(Visit.id == visit_id)
            .values(created_at=LAST_WEEK.start + timedelta(hours=6))
        )
    with session_scope() as session:
        review.confirm(session, lambda: pack, org.org_id, org.rep_id, visit_id)
    return visit_id


def _insights(session: Any, visit_id: str) -> list[str]:
    return list(session.scalars(select(Insight.id).where(Insight.visit_id == visit_id)))


def _issue_of(session: Any, insight_id: str) -> str:
    return session.scalars(
        select(InsightRoute.issue_id).where(InsightRoute.insight_id == insight_id)
    ).one()


def demo_world() -> World:
    """The demo org (the pepsico pack): finance first, distribution second."""
    demo = seeded_demo()
    pack = load_org_pack(*DEMO_PACK)
    corner = shop(demo, "Corner Daily Needs")
    lotus = shop(demo, "Lotus General Store")
    return build(corner, pack, ("finance", "distribution"), lotus, ORG_PACKS_DIR)


def tiny_world() -> World:
    """Another org (the tiny pack): money first, field second."""
    org = tiny_org("Another Org")
    far = at_shop(org, "Far Shop", area="Far Area")
    return build(org, tiny(), ("money", "field"), far, ORG_PACK_FIXTURES)


# --- the cases -------------------------------------------------------------------------------

Request = Callable[[World], tuple[str, dict[str, Any]]]


@dataclass(frozen=True)
class Case:
    method: str
    template: str
    request: Request
    # Role: OK, or (status, code).
    expected: dict[str, Any]
    name: str = ""


def case(
    method: str,
    template: str,
    request: Request,
    ok: tuple[str, ...],
    no: str = "not_found",
    *,
    forbidden: tuple[str, ...] = (),
    forbid: str = "not_your_visit",
    no_for: dict[str, str] | None = None,
    name: str = "",
) -> Case:
    """`ok` roles get through; `forbidden` get 403 `forbid`; the rest 404 `no` (or no_for's)."""
    expected: dict[str, Any] = {}
    for role in ROLES:
        if role in ok:
            expected[role] = OK
        elif role in forbidden:
            expected[role] = (403, forbid)
        else:
            expected[role] = (404, (no_for or {}).get(role, no))
    return Case(method, template, request, expected, name or f"{method} {template}")


def at(path: str, **kwargs: Any) -> Request:
    """A request: the path, formatted with the world's fields, and httpx's other arguments."""
    return lambda w: (path.format(w=w), {k: v(w) if callable(v) else v for k, v in kwargs.items()})


EVERYONE = ROLES
REPS = ("rep", "other_rep")
WHOLE = ("manager", "admin")
THE_REP_AND_READERS = ("rep", "first", *WHOLE)  # who reads the rep's confirmed visit
ONE = b"x"
SEGMENT = {"content-length": "1", "x-content-sha256": __import__("hashlib").sha256(ONE).hexdigest()}
NEW_INSIGHT = {
    "turn_id": 1,
    "kind": "problem",
    "category": "payments",
    "severity": "low",
    "summary_en": "The shop is owed money.",
}
TEAM_SCOPE = {"no_for": {"second": "team_not_found"}}


def new_visit_body(w: World) -> dict[str, Any]:
    return {"shop_id": w.shop_id, "kind": "conversation", "client": "web"}


def new_insight_body(w: World) -> dict[str, Any]:
    return {"id": str(uuid.uuid4()), **NEW_INSIGHT}


def new_note_body(w: World) -> dict[str, Any]:
    return {"id": str(uuid.uuid4()), "text": "Call back."}


def teams_body(w: World) -> dict[str, Any]:
    """The review insight to the first team, naming its routing version as it is now."""
    with session_scope(read_only=True) as session:
        version = routing.version_of(session, w.review_insight)
    return {"team_ids": [w.teams["first"]], "routing_version": version}


def team_page(w: World) -> dict[str, Any]:
    return {"scope": "team", "period": "week", "team_id": w.teams["first"]}


CASES = [
    case("GET", "/api/auth/me", at("/api/auth/me"), EVERYONE),
    # Batch 17b: a rep's own counts this month, and only a rep's.
    case("GET", "/api/me/counts", at("/api/me/counts"), REPS),
    # Batch F2: the rep's home's counts, a rep's own.
    case("GET", "/api/me/home", at("/api/me/home"), REPS),
    # Batch F3: the rep's own problems still being worked on.
    case("GET", "/api/me/pending", at("/api/me/pending"), REPS),
    # Batch 17e: every shop's fixes since its last visit, for the rep's Updates page.
    case("GET", "/api/me/fixed", at("/api/me/fixed"), (*REPS, *WHOLE)),
    # The home's counts for whoever works issues: a team member's team's, or the whole org's.
    case("GET", "/api/me/overview", at("/api/me/overview"), ("first", "second", *WHOLE)),
    # Batch 17d: a question for the whole org (a team member's: their team's). No key here: 503.
    case(
        "POST",
        "/api/ask",
        at("/api/ask", json={"question": "Late deliveries?"}),
        ("first", "second", *WHOLE),
    ),
    # Everyone: a team member's visit page names its shop.
    case("GET", "/api/shops", at("/api/shops"), EVERYONE),
    # Batch 17b: what's been fixed for a shop since its last visit, for the org's own staff.
    case("GET", "/api/shops/{shop_id}/fixed", at("/api/shops/{w.shop_id}/fixed"), (*REPS, *WHOLE)),
    # Batch 17: the org's own staff share a shop's consent link or mark its paper form.
    case(
        "POST",
        "/api/shops/{shop_id}/consent-links",
        at("/api/shops/{w.shop_id}/consent-links"),
        (*REPS, *WHOLE),
    ),
    case(
        "POST",
        "/api/shops/{shop_id}/consent",
        at("/api/shops/{w.shop_id}/consent", json={"language": "hi"}),
        (*REPS, *WHOLE),
    ),
    case(
        "POST",
        "/api/visits",
        at(
            "/api/visits",
            json=new_visit_body,
        ),
        REPS,
    ),
    case("GET", "/api/visits", at("/api/visits"), (*REPS, *WHOLE)),
    case(
        "GET",
        "/api/visits/{visit_id}",
        at("/api/visits/{w.confirmed}"),
        THE_REP_AND_READERS,
        "visit_not_found",
    ),
    case(
        "GET",
        "/api/visits/{visit_id}/transcript",
        at("/api/visits/{w.confirmed}/transcript"),
        THE_REP_AND_READERS,
        "visit_not_found",
    ),
    case(
        "GET",
        "/api/visits/{visit_id}/audio",
        at("/api/visits/{w.confirmed}/audio"),
        THE_REP_AND_READERS,
        "visit_not_found",
    ),
    case(
        "GET",
        "/api/visits/{visit_id}/review",
        at("/api/visits/{w.confirmed}/review"),
        THE_REP_AND_READERS,
        "visit_not_found",
    ),
    case(
        "PUT",
        "/api/visits/{visit_id}/segments/{seq}",
        at("/api/visits/{w.recording}/segments/0", content=ONE, headers=SEGMENT),
        ("rep",),
        "visit_not_found",
        forbidden=WHOLE,
    ),
    case(
        "POST",
        "/api/visits/{visit_id}/complete",
        at("/api/visits/{w.recording}/complete", json={"last_seq": 0, "ext": "webm"}),
        ("rep",),
        "visit_not_found",
        forbidden=WHOLE,
    ),
    case(
        "POST",
        "/api/visits/{visit_id}/reprocess",
        at("/api/visits/{w.confirmed}/reprocess", json={"from": "extract"}),
        ("rep",),
        "visit_not_found",
        forbidden=("first", *WHOLE),
    ),
    case(
        "POST",
        "/api/visits/{visit_id}/insights",
        at(
            "/api/visits/{w.review}/insights",
            json=new_insight_body,
        ),
        ("rep",),
        "visit_not_found",
        forbidden=WHOLE,
    ),
    case(
        "POST",
        "/api/visits/{visit_id}/notes",
        at(
            "/api/visits/{w.review}/notes",
            json=new_note_body,
        ),
        ("rep",),
        "visit_not_found",
        forbidden=WHOLE,
    ),
    case(
        "POST",
        "/api/visits/{visit_id}/confirm",
        at("/api/visits/{w.review}/confirm"),
        ("rep",),
        "visit_not_found",
        forbidden=WHOLE,
    ),
    case(
        "PATCH",
        "/api/insights/{insight_id}",
        at("/api/insights/{w.review_insight}", json={"severity": "high"}),
        ("rep",),
        "insight_not_found",
        forbidden=WHOLE,
    ),
    case(
        "DELETE",
        "/api/insights/{insight_id}",
        at("/api/insights/{w.review_insight}"),
        ("rep",),
        "insight_not_found",
        forbidden=WHOLE,
    ),
    # Batch F1: the visit's rep picks a finding's teams in review, as other review edits.
    case(
        "PUT",
        "/api/insights/{insight_id}/teams",
        at("/api/insights/{w.review_insight}/teams", json=lambda w: teams_body(w)),
        ("rep",),
        "insight_not_found",
        forbidden=WHOLE,
    ),
    case(
        "POST",
        "/api/insights/{insight_id}/accept",
        at("/api/insights/{w.routed_insight}/accept", json={"slot": 1, "version": 1}),
        ("first", *WHOLE),
        "insight_not_found",
        forbidden=("rep",),
        forbid="not_your_team",
    ),
    case(
        "POST",
        "/api/insights/{insight_id}/move",
        at(
            "/api/insights/{w.routed_insight}/move",
            json=lambda w: {"slot": 1, "version": 1, "team_id": w.teams["second"]},
        ),
        ("first", *WHOLE),
        "insight_not_found",
        forbidden=("rep",),
        forbid="not_your_team",
    ),
    case(
        "POST",
        "/api/insights/{insight_id}/also-send",
        at(
            "/api/insights/{w.routed_insight}/also-send",
            json=lambda w: {"slot": 1, "version": 1, "team_id": w.teams["second"]},
        ),
        ("first", *WHOLE),
        "insight_not_found",
        forbidden=("rep",),
        forbid="not_your_team",
    ),
    case(
        "POST",
        "/api/insights/{insight_id}/team",
        at("/api/insights/{w.triage_insight}/team", json=lambda w: {"team_id": w.teams["first"]}),
        WHOLE,
        "insight_not_found",
        forbidden=("rep",),
        forbid="not_a_manager",
    ),
    case("GET", "/api/issues/options", at("/api/issues/options"), ("first", "second", *WHOLE)),
    case("GET", "/api/issues/triage", at("/api/issues/triage"), WHOLE),
    case(
        "GET",
        "/api/issues",
        at("/api/issues", params=lambda w: {"team": w.keys["first"]}),
        ("first", *WHOLE),
        **TEAM_SCOPE,
    ),
    case(
        "GET",
        "/api/issues/{issue_id}",
        at("/api/issues/{w.issue}"),
        ("first", *WHOLE),
        "issue_not_found",
    ),
    case(
        "PATCH",
        "/api/issues/{issue_id}",
        at("/api/issues/{w.issue}", json={"version": 1, "status": "in_progress"}),
        ("first", *WHOLE),
        "issue_not_found",
    ),
    case(
        "POST",
        "/api/issues/suggestions/{suggestion_id}/merge",
        at(
            "/api/issues/suggestions/{w.suggestion}/merge",
            json={"into_version": 1, "merge_version": 1},
        ),
        ("first", *WHOLE),
        "suggestion_not_found",
    ),
    case(
        "POST",
        "/api/issues/suggestions/{suggestion_id}/dismiss",
        at("/api/issues/suggestions/{w.suggestion}/dismiss"),
        ("first", *WHOLE),
        "suggestion_not_found",
    ),
    case(
        "GET",
        "/api/summaries",
        at(
            "/api/summaries",
            params=team_page,
        ),
        ("first", *WHOLE),
        **TEAM_SCOPE,
    ),
    case(
        "GET",
        "/api/summaries",
        at("/api/summaries", params={"scope": "org", "period": "month"}),
        WHOLE,
        name="GET /api/summaries (the org's)",
    ),
    case(
        "POST",
        "/api/summaries",
        at(
            "/api/summaries",
            json=team_page,
        ),
        ("first", *WHOLE),
        **TEAM_SCOPE,
    ),
    case(
        "POST",
        "/api/summaries",
        at("/api/summaries", json={"scope": "org", "period": "month"}),
        WHOLE,
        name="POST /api/summaries (the org's)",
    ),
    case(
        "GET",
        "/api/summaries/drill",
        at(
            "/api/summaries/drill",
            params=lambda w: {"team_id": w.teams["first"], "issue_id": w.issue, "period": "week"},
        ),
        ("first", *WHOLE),
        **TEAM_SCOPE,
    ),
    case(
        "GET",
        "/api/summaries/{summary_id}",
        at("/api/summaries/{w.team_summary}"),
        ("first", *WHOLE),
        "summary_not_found",
    ),
    case(
        "GET",
        "/api/summaries/{summary_id}",
        at("/api/summaries/{w.org_summary}"),
        WHOLE,
        "summary_not_found",
        name="GET /api/summaries/{summary_id} (the org's)",
    ),
    case(
        "POST",
        "/api/summaries/{summary_id}/retry",
        at("/api/summaries/{w.team_summary}/retry"),
        ("first", *WHOLE),
        "summary_not_found",
    ),
    case(
        "POST",
        "/api/summaries/{summary_id}/retry",
        at("/api/summaries/{w.org_summary}/retry"),
        WHOLE,
        "summary_not_found",
        name="POST /api/summaries/{summary_id}/retry (the org's)",
    ),
    # The catalog screen (batch 16b): admins only.
    case("GET", "/api/catalog/items", at("/api/catalog/items", params={"q": "a"}), ("admin",)),
    case(
        "POST",
        "/api/catalog/items",
        at("/api/catalog/items", json={"kind": "competitor", "company": "Zorb", "brand": "Zorb"}),
        ("admin",),
    ),
    case(
        "PATCH",
        "/api/catalog/items/{item_id}",
        at(
            "/api/catalog/items/{w.catalog_item}",
            json={"version": 1, "company": "Made Up Co", "brand": "Made Up", "active": False},
        ),
        ("admin",),
    ),
    case(
        "POST",
        "/api/catalog/items/{item_id}/aliases",
        at("/api/catalog/items/{w.catalog_item}/aliases", json={"text": "Zorbo"}),
        ("admin",),
    ),
    case(
        "DELETE",
        "/api/catalog/aliases/{alias_id}",
        at("/api/catalog/aliases/{w.catalog_alias}"),
        ("admin",),
    ),
    case("GET", "/api/catalog/reviews", at("/api/catalog/reviews"), ("admin",)),
    case(
        "POST",
        "/api/catalog/reviews/{review_id}/resolve",
        at(
            "/api/catalog/reviews/{w.alias_review}/resolve",
            json={"action": "create", "item": {"company": "Zorb Co", "brand": "Zorbix"}},
        ),
        ("admin",),
        name="POST /api/catalog/reviews/{review_id}/resolve (create)",
    ),
    case(
        "POST",
        "/api/catalog/reviews/{review_id}/resolve",
        at("/api/catalog/reviews/{w.alias_review}/resolve", json={"action": "dismiss"}),
        ("admin",),
    ),
]


def outcome(response: httpx.Response) -> Any:
    """OK, or the refusal: (status, code)."""
    if response.status_code in (401, 403, 404):
        code = response.json()["error"]["code"]
        if response.status_code == 401 or code in NOT_FOUND | FORBIDDEN:
            return (response.status_code, code)
    return OK


def send(client: TestClient, found: Case, world: World) -> httpx.Response:
    path, kwargs = found.request(world)
    return client.request(found.method, path, **kwargs)


# --- the tests -------------------------------------------------------------------------------


def test_every_route_is_in_the_matrix() -> None:
    """A new route fails here until its row says who may use it (16b's catalog too)."""
    covered = {(c.method, c.template) for c in CASES}

    assert covered == set(api_routes(create_app())) - PUBLIC


@pytest.mark.parametrize("found", CASES, ids=[c.name for c in CASES])
def test_each_role_gets_what_the_matrix_says(client: TestClient, found: Case) -> None:
    world = demo_world()
    refused_first = sorted(ROLES, key=lambda role: found.expected[role] == OK)
    got = {}
    for role in refused_first:
        sign_in(client, world.users[role])
        got[role] = outcome(send(client, found, world))

    assert got == found.expected


def test_another_org_s_objects_are_404_for_everyone() -> None:
    """Two orgs: every route given the other org's IDs (visits, insights, issues, a
    suggestion, summaries, teams, a shop) answers 404 to each of this org's roles, the whole
    org's manager and admin included; nothing of the other org changes."""
    ours, theirs = demo_world(), tiny_world()
    with TestClient(create_app(), base_url="https://testserver") as client:
        for found in CASES:
            if found.request(ours) == found.request(theirs):
                continue  # no object of an org's in the request
            for role in ROLES:
                sign_in(client, ours.users[role])
                response = send(client, found, theirs)
                assert response.status_code == 404, (found.name, role, response.text)


def test_a_rep_lists_only_their_own_visits(client: TestClient) -> None:
    world = demo_world()
    sign_in(client, world.users["other_rep"])

    assert client.get("/api/visits").json()["items"] != []  # their supply visit
    shown = {v["rep_id"] for v in client.get("/api/visits").json()["items"]}
    assert shown == {world.users["other_rep"]}


def test_a_team_member_s_inbox_is_their_own_team_s(client: TestClient) -> None:
    world = demo_world()
    sign_in(client, world.users["first"])

    cards = client.get("/api/issues").json()  # no team named: their own

    assert {card["team_key"] for card in cards} == {"finance"}
    assert client.get("/api/issues", params={"team": "finance"}).json() == cards


def test_a_team_member_sees_only_their_team_s_part_of_a_visit(client: TestClient) -> None:
    """The rep's notes, report, tasks and order, and other teams' findings and routes, stay
    the rep's and the whole org's."""
    world = demo_world()
    sign_in(client, world.users["rep"])
    base = f"/api/visits/{world.review}"
    supply = {**new_insight_body(world), "category": "supply", "summary_en": "Late again."}
    assert client.post(f"{base}/insights", json=supply).status_code == 201
    assert client.post(f"{base}/notes", json=new_note_body(world)).status_code == 201
    assert client.post(f"{base}/confirm").status_code == 200

    seen = {}
    for role in ("first", "second", "manager"):
        sign_in(client, world.users[role])
        found = client.get(f"{base}/review").json()
        seen[role] = (
            sorted(i["category"] for i in found["insights"]),
            sorted({r["team_key"] for i in found["insights"] for r in i["routes"]}),
            len(found["notes"]),
        )

    assert seen == {
        "first": (["payments"], ["finance"], 0),
        "second": (["supply"], ["distribution"], 0),
        "manager": (["payments", "supply"], ["distribution", "finance"], 1),
    }


# --- R4's guard: a null where a value belongs is a 4xx, never a 5xx --------------------------


def nulled(found: Case, world: World) -> list[tuple[str, tuple[str, dict[str, Any]]]]:
    """The case's valid request again with each of its body's top-level fields null."""
    path, kwargs = found.request(world)
    body = kwargs.get("json")
    if not isinstance(body, dict):
        return []
    return [(name, (path, {**kwargs, "json": {**body, name: None}})) for name in body]


@pytest.mark.parametrize(
    "found",
    [c for c in CASES if c.method in ("POST", "PATCH", "PUT")],
    ids=[c.name for c in CASES if c.method in ("POST", "PATCH", "PUT")],
)
def test_a_null_field_in_a_write_is_never_a_server_error(client: TestClient, found: Case) -> None:
    """R4, which came up in 16-1, 16-3 and 16b-1: each write route's valid body, as a role it
    lets through, with each top-level field null in turn. A later route is covered by its
    matrix row."""
    [allowed, *_] = [role for role in ROLES if found.expected[role] == OK]
    world = demo_world()
    errors = []
    for index in range(len(nulled(found, world))):
        sign_in(client, world.users[allowed])
        name, (path, kwargs) = nulled(found, world)[index]
        response = client.request(found.method, path, **kwargs)
        if response.status_code >= 500:
            errors.append(f"{name}: {response.status_code} {response.text[:120]}")
        if response.status_code < 300:
            wipe_database()  # it changed something: the next null meets a fresh world
            world = demo_world()

    assert errors == []
