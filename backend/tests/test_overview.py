"""GET /api/me/overview: the home's counts for whoever works issues. The demo org on the
PepsiCo India example pack (test_inbox_api): payments go to finance and supply to
distribution, an issue per distributor; "other" has no team, so it waits in triage."""

from datetime import datetime, timedelta
from functools import partial
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from countertalk import entities, extraction, issues
from countertalk.db import session_scope
from countertalk.models import (
    AliasReview,
    AliasReviewStatus,
    CatalogKind,
    InsightRoute,
    Issue,
    Role,
    Visit,
    VisitStatus,
    utc_now,
)
from countertalk.summaries import week_start
from tests.conftest import a_user, sign_in
from tests.test_extraction import insight, transcribed
from tests.test_inbox_api import OTHER, change_visit, confirmed, shop, team_id
from tests.test_inbox_routing import only_insight
from tests.test_review_api import error_of, signed_in_manager
from tests.test_summaries import resolve
from tests.test_visits import Org_, new_visit

SUPPLY = insight(category="supply", severity="low", suggested_team="distribution")
# What a team member's overview leaves out.
NOT_THEIRS = {"triage": None, "visits_this_week": None, "names_to_review": None}


@pytest.fixture(autouse=True)
def demo(client: TestClient) -> Org_:
    return signed_in_manager(client)


def overview(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/me/overview")
    assert response.status_code == 200, response.text
    return response.json()


def issue_of(visit_id: str) -> str:
    """The issue the visit's one insight went to (its one team's)."""
    with session_scope(read_only=True) as session:
        query = select(InsightRoute.issue_id).where(
            InsightRoute.insight_id == only_insight(visit_id)
        )
        return session.scalars(query).one()


def start(demo: Org_, issue_id: str) -> None:
    """In progress, as the inbox changes it."""
    with session_scope() as session:
        version = session.get_one(Issue, issue_id).version
        issues.change(session, demo.org_id, demo.rep_id, issue_id, version, status="in_progress")


def last_week() -> datetime:
    """Last Sunday's last minute, in IST."""
    return week_start(utc_now()) - timedelta(minutes=1)


def a_busy_week(client: TestClient, demo: Org_) -> None:
    """Finance's three issues (d3 open, d1 in progress, d2 resolved today), all past their due
    date; distribution's two (d3 open, d1 resolved last week); one insight in triage. Eight
    visits: one of them last week's, one failed, and one still recording (never sent)."""
    new_visit(shop(demo, "Lotus General Store"))  # still recording: not a visit made
    corner = confirmed(client, shop(demo, "Corner Daily Needs"))  # finance, d3
    sunrise = confirmed(client, shop(demo, "Sunrise Kirana"))  # finance, d1
    lotus = confirmed(client, shop(demo, "Lotus General Store"))  # finance, d2
    confirmed(client, shop(demo, "Moonlight Provisions"), SUPPLY)  # distribution, d3
    earlier = confirmed(client, shop(demo, "Sunrise Kirana"), SUPPLY)  # distribution, d1
    confirmed(client, shop(demo, "Moonlight Provisions"), OTHER)  # no team: triage
    start(demo, issue_of(sunrise))
    resolve(demo, issue_of(lotus), utc_now())
    resolve(demo, issue_of(earlier), last_week())
    finance = [issue_of(visit_id) for visit_id in (corner, sunrise, lotus)]
    failed = transcribed(shop(demo, "Corner Daily Needs"))
    with session_scope() as session:
        past = utc_now() - timedelta(days=1)
        session.execute(update(Issue).where(Issue.id.in_(finance)).values(due_at=past))
        session.execute(update(Visit).where(Visit.id == failed).values(status=VisitStatus.FAILED))
    change_visit(earlier, last_week())


def test_a_manager_counts_the_whole_org(client: TestClient, demo: Org_) -> None:
    a_busy_week(client, demo)

    assert overview(client) == {
        "open": 2,  # finance's d3 and distribution's d3
        "in_progress": 1,
        "overdue": 2,  # the open one and the one in progress, not the one resolved
        "resolved_this_week": 1,  # not distribution's, resolved last week
        "triage": 1,
        "visits_this_week": 5,  # not last week's, nor the failed one
        "names_to_review": None,  # an admin's only
    }


def test_a_team_member_counts_only_their_team_s_issues(client: TestClient, demo: Org_) -> None:
    a_busy_week(client, demo)
    sign_in(client, a_user(demo.org_id, Role.TEAM_MEMBER, "Finance Member", "finance"))
    finance = overview(client)
    sign_in(client, a_user(demo.org_id, Role.TEAM_MEMBER, "Distribution Member", "distribution"))
    distribution = overview(client)

    assert finance == {
        "open": 1,
        "in_progress": 1,
        "overdue": 2,
        "resolved_this_week": 1,
        **NOT_THEIRS,
    }
    assert distribution == {
        "open": 1,
        "in_progress": 0,
        "overdue": 0,
        "resolved_this_week": 0,
        **NOT_THEIRS,
    }


def test_an_admin_also_gets_the_names_waiting_for_review(client: TestClient, demo: Org_) -> None:
    said_in = [transcribed(shop(demo, name)) for name in ("Corner Daily Needs", "Sunrise Kirana")]

    def review(visit_id: str, kind: str, text: str, status: str = AliasReviewStatus.OPEN) -> Any:
        normalised = entities.link_key(text)
        return AliasReview(
            org_id=demo.org_id,
            kind=kind,
            text=text,
            normalised=normalised,
            visit_id=visit_id,
            status=status,
        )

    with session_scope() as session:
        session.add_all(
            [
                review(said_in[0], CatalogKind.COMPETITOR, "Bikano"),
                review(said_in[1], CatalogKind.COMPETITOR, "Bikano"),  # the same name: one
                review(said_in[0], CatalogKind.OWN, "Cheetos Puffs"),
                review(said_in[1], CatalogKind.COMPETITOR, "Prabhuji", AliasReviewStatus.DISMISSED),
            ]
        )
    manager = overview(client)
    sign_in(client, a_user(demo.org_id, Role.ADMIN, "Demo Admin"))

    found = overview(client)

    assert found == {**manager, "names_to_review": 2}
    assert len(client.get("/api/catalog/reviews").json()) == 2  # as the catalog screen lists them
    assert (manager["visits_this_week"], manager["names_to_review"]) == (2, None)


def test_a_merged_or_emptied_issue_isn_t_counted(client: TestClient, demo: Org_) -> None:
    corner = confirmed(client, shop(demo, "Corner Daily Needs"))  # finance, d3
    sunrise = confirmed(client, shop(demo, "Sunrise Kirana"))  # finance, d1
    lotus = confirmed(client, shop(demo, "Lotus General Store"))  # finance, d2
    assert overview(client)["open"] == 3
    kept, merged = issue_of(corner), issue_of(sunrise)

    with session_scope() as session:
        pack = partial(extraction.pack_for, demo.org_id, session=session)
        versions = {i: session.get_one(Issue, i).version for i in (kept, merged)}
        into, gone = (kept, versions[kept]), (merged, versions[merged])
        issues.merge(session, pack, demo.org_id, demo.rep_id, into, gone)
    moved = client.post(
        f"/api/insights/{only_insight(lotus)}/move",
        json={"slot": 1, "version": 1, "team_id": team_id(client, "distribution")},
    )

    assert moved.status_code == 200, moved.text
    # Finance's d1 is merged, and its d2 has no insight left: d3, and distribution's new one.
    assert overview(client)["open"] == 2


def test_a_rep_gets_404(client: TestClient, demo: Org_) -> None:
    sign_in(client, demo.rep_id)

    assert error_of(client.get("/api/me/overview")) == (404, "not_found")
