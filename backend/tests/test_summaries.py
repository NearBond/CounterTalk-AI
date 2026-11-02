"""summaries.py's numbers (batch 15b): K6's planted patterns, as the fixture expects them, and the
trend rule at its edges."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest import mock

import pytest
from sqlalchemy import select, update

from countertalk import issues, limits, review
from countertalk.db import session_scope
from countertalk.models import InsightRoute, Issue, Shop, Team, Visit
from countertalk.summaries import (
    CategoryArea,
    CompetitorCount,
    IssueNumbers,
    Measures,
    OrgNumbers,
    Totals,
    age_weeks,
    check,
    last_complete,
    org_numbers,
    period_of,
    poisson_at_least,
    team_numbers,
    trend,
)
from scripts import _k6
from tests.test_extraction import insight
from tests.test_review import PACK, reviewed
from tests.test_roles import tiny_org
from tests.test_visits import Org_

EXPECTED = _k6.expected()


@pytest.fixture
def world() -> _k6.World:
    return _k6.load()


def summary(world: _k6.World, team: str, week: int) -> list[IssueNumbers]:
    with session_scope(read_only=True) as session:
        found = team_numbers(session, world.pack, world.org_id, world.teams[team], world.week(week))
        return found.issues


def row(numbers: list[IssueNumbers], issue_id: str) -> tuple[int, Measures]:
    """Its rank (from 1) and its measures."""
    return next(
        (rank, n.measures) for rank, n in enumerate(numbers, start=1) if n.issue_id == issue_id
    )


def test_k6_before_15c_s_merge(world: _k6.World) -> None:
    before = EXPECTED["before_merge"]
    p1 = _k6.issue_of(world, world.insights["P1"][0], "finance")
    p1_e = _k6.issue_of(world, world.insights["P1"][4], "finance")  # E's: its scheme isn't linked
    p2 = _k6.issue_of(world, world.insights["P2"][0], "distribution")
    p3 = _k6.issue_of(world, world.insights["P3"][0], "finance")

    finance_w5, finance_w6 = summary(world, "finance", 5), summary(world, "finance", 6)
    distribution_w5 = summary(world, "distribution", 5)
    distribution_w6 = summary(world, "distribution", 6)

    # P1: A-D's issue, new in W5, growing in W6 and first; E's issue apart.
    _, w5 = row(finance_w5, p1)
    assert (w5.label, w5.shops) == (
        before["P1"]["W5"]["label"],
        before["P1"]["W5"]["shops_in_week"],
    )
    rank, w6 = row(finance_w6, p1)
    want = before["P1"]["W6"]
    assert (w6.label, w6.shops, w6.insights, w6.reps, w6.baseline) == (
        want["label"],
        want["shops_in_week"],
        want["insights_in_week"],
        want["reps_in_week"],
        want["baseline"],
    )
    assert (w6.open_shops, w6.raised_again, w6.age_weeks, w6.score, rank) == (
        want["open_shops"],
        want["raised_again"],
        want["age_weeks"],
        want["score"],
        want["finance_rank"],
    )
    _, e = row(finance_w6, p1_e)
    want = before["P1_E"]["W6"]
    assert (e.label, e.open_shops, e.age_weeks, e.score) == (
        want["label"],
        want["open_shops"],
        want["age_weeks"],
        want["score"],
    )
    # P2: steady in W5 at 2 shops, growing in W6 and first in distribution.
    _, w5 = row(distribution_w5, p2)
    assert (w5.label, w5.shops) == (
        before["P2"]["W5"]["label"],
        before["P2"]["W5"]["shops_in_week"],
    )
    rank, w6 = row(distribution_w6, p2)
    want = before["P2"]["W6"]
    assert (w6.label, w6.shops, w6.baseline, w6.open_shops, w6.age_weeks, w6.score, rank) == (
        want["label"],
        want["shops_in_week"],
        want["baseline"],
        want["open_shops"],
        want["age_weeks"],
        want["score"],
        want["distribution_rank"],
    )
    # P3: reopened, not a new issue: recurring in W5 and W6, second in finance.
    assert row(finance_w5, p3)[1].label == before["P3"]["W5"]["label"]
    rank, w6 = row(finance_w6, p3)
    want = before["P3"]["W6"]
    assert (w6.label, w6.open_shops, w6.age_weeks, w6.score, rank) == (
        want["label"],
        want["open_shops"],
        want["age_weeks"],
        want["score"],
        want["finance_rank"],
    )
    # W6's recurring is the reopen's alone: it was seen in only W2 and W5 of the four before.
    assert w6.seen_periods == 2
    # In W3 it was resolved, with nothing new since; in W4 it has nothing to say.
    assert row(summary(world, "finance", 3), p3)[1].label == "resolved"
    assert p3 not in {n.issue_id for n in summary(world, "finance", 4)}


def test_k6_s_noise_never_grows_or_outranks_a_pattern(world: _k6.World) -> None:
    written = EXPECTED["written"]["P4"]
    patterns = {
        _k6.issue_of(world, insight_id, team)
        for pattern, team in (("P1", "finance"), ("P2", "distribution"), ("P3", "finance"))
        for insight_id in world.insights[pattern]
    } | {_k6.issue_of(world, world.insights["P3"][0], "area_manager")}
    patterns |= {
        _k6.issue_of(world, world.insights["P5"][0], t) for t in ("finance", "area_manager")
    }

    for team in world.teams:
        for week in range(1, 7):
            noise = [n for n in summary(world, team, week) if n.issue_id not in patterns]
            assert [n for n in noise if n.measures.label == "growing"] == []
            assert (
                max((n.measures.open_shops for n in noise), default=0)
                <= written["most_shops_in_an_issue"]
            )
            highest = written[
                "highest_finance_score" if team == "finance" else "highest_other_score"
            ]
            assert max((n.measures.score for n in noise), default=0) <= highest
    # W6: P1 and P3 head finance's list, P2 distribution's.
    assert [n.issue_id for n in summary(world, "finance", 6)[:2]] == [
        _k6.issue_of(world, world.insights["P1"][0], "finance"),
        _k6.issue_of(world, world.insights["P3"][0], "finance"),
    ]
    assert summary(world, "distribution", 6)[0].issue_id == _k6.issue_of(
        world, world.insights["P2"][0], "distribution"
    )


def test_k6_s_p5_is_one_insight_in_each_of_its_two_teams(world: _k6.World) -> None:
    written = EXPECTED["written"]["P5"]
    [insight_id] = world.insights["P5"]

    for team in written["teams"]:
        _, numbers = row(summary(world, team, 6), _k6.issue_of(world, insight_id, team))
        assert (numbers.insights, numbers.shops, numbers.score) == (
            written["W6_each_team"]["insights"],
            written["W6_each_team"]["shops"],
            written["W6_score"],
        )


def test_every_route_of_a_confirmed_insight_is_in_one_issue_of_its_own_team(
    world: _k6.World,
) -> None:
    with session_scope(read_only=True) as session:
        routes = session.execute(
            select(InsightRoute.team_id, Issue.team_id)
            .outerjoin(Issue, Issue.id == InsightRoute.issue_id)
            .where(InsightRoute.org_id == world.org_id)
        ).all()

    assert len(routes) > 60
    assert [(route, issue) for route, issue in routes if route != issue] == []


# --- the trend rule at its edges -------------------------------------------------------------


def label(**changes: object) -> str:
    found = {
        "shops": 0,
        "baseline": 0.0,
        "seen_periods": 0,
        "first_seen_in_period": False,
        "reopened": False,
        "resolved_in_period": False,
    } | changes
    return trend(**found)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("shops", "baseline", "expected"),
    [
        (2, 0.0, "steady"),  # 2 shops is never growing, whatever the baseline
        (3, 0.5, "growing"),  # p of 3+ at 0.5 is about 1.4%
        (3, 1.0, "steady"),  # p of 3+ at 1.0 is about 8%
        (3, 0.0, "growing"),  # the baseline is floored at 0.5
        (4, 1.0, "growing"),  # K6's P2: about 1.9%
    ],
)
def test_growing_needs_three_shops_and_a_small_chance_at_the_baseline(
    shops: int, baseline: float, expected: str
) -> None:
    assert label(shops=shops, baseline=baseline) == expected


def test_the_first_rule_that_holds_wins() -> None:
    assert label(reopened=True, first_seen_in_period=True, shops=5) == "recurring"
    assert label(resolved_in_period=True, reopened=True) == "resolved"
    assert label(seen_periods=3) == "recurring"
    assert label(seen_periods=2, first_seen_in_period=True) == "new"
    assert label(first_seen_in_period=True, shops=9) == "new"
    # K6's P2: seen three periods running, then a jump: growing, not recurring.
    assert label(seen_periods=3, shops=4, baseline=1.0) == "growing"
    assert label(seen_periods=3, shops=2, baseline=1.0) == "recurring"


def test_three_shops_are_needed_whatever_the_chance(monkeypatch: pytest.MonkeyPatch) -> None:
    # With the floor, 2 shops never reach 5% (about 9%): lowered, they would, but for the minimum.
    monkeypatch.setattr(limits, "BASELINE_FLOOR", 0.1)

    assert label(shops=2, baseline=0.1) == "steady"


def test_the_baseline_is_floored(monkeypatch: pytest.MonkeyPatch) -> None:
    # 2 shops at 0.1 is about 0.5%: at the floor of 0.5, about 9%.
    monkeypatch.setattr(limits, "GROWING_MIN_SHOPS", 2)

    assert label(shops=2, baseline=0.1) == "steady"


def test_age_counts_whole_weeks_from_the_week_first_seen_to_the_period_s_start() -> None:
    # First seen Wednesday 16 Sep: its week began Monday 14 Sep, 2 whole weeks before October.
    first_seen = datetime(2026, 9, 16, 5, 30, tzinfo=UTC)

    assert age_weeks(first_seen, period_of("month", date(2026, 10, 1))) == 2
    assert age_weeks(first_seen, period_of("week", date(2026, 9, 16))) == 0


def test_the_poisson_chance() -> None:
    assert poisson_at_least(0, 0.5) == 1.0
    assert round(poisson_at_least(3, 0.5), 4) == 0.0144
    assert round(poisson_at_least(4, 1.0), 4) == 0.0190


# --- periods ---------------------------------------------------------------------------------


def test_a_week_runs_monday_to_monday_and_a_month_is_its_calendar_month_in_ist() -> None:
    week = period_of("week", date(2026, 10, 28))  # a Wednesday
    month = period_of("month", date(2026, 10, 28))

    assert (week.start, week.end) == (
        datetime(2026, 10, 25, 18, 30, tzinfo=UTC),  # Monday 26 Oct, 00:00 IST
        datetime(2026, 11, 1, 18, 30, tzinfo=UTC),
    )
    assert (month.start, month.end) == (
        datetime(2026, 9, 30, 18, 30, tzinfo=UTC),
        datetime(2026, 10, 31, 18, 30, tzinfo=UTC),
    )


def test_last_week_and_last_month_are_the_latest_that_have_ended() -> None:
    # Monday 2 Nov, 01:00 IST: last week is 26 Oct to 2 Nov, last month October.
    now = datetime(2026, 11, 1, 19, 30, tzinfo=UTC)

    assert last_complete("week", now) == period_of("week", date(2026, 10, 26))
    assert last_complete("month", now) == period_of("month", date(2026, 10, 1))


# --- read in its visits' time ----------------------------------------------------------------


def at(moment: datetime) -> Any:
    """issues.py's clock, at a moment."""
    return mock.patch.object(issues, "utc_now", lambda: moment)


def visit_at(org: Org_, when: datetime, confirmed_at: datetime, *found: dict[str, Any]) -> str:
    """A visit dated `when`, confirmed at `confirmed_at`: its ID."""
    visit_id, _ = reviewed(org, *found)
    with session_scope() as session:
        session.execute(update(Visit).where(Visit.id == visit_id).values(created_at=when))
    with at(confirmed_at), session_scope() as session:
        review.confirm(session, lambda: PACK, org.org_id, org.rep_id, visit_id)
    return visit_id


def money_numbers(org: Org_, period_day: date) -> list[IssueNumbers]:
    with session_scope(read_only=True) as session:
        money = session.scalars(
            select(Team.id).where(Team.org_id == org.org_id, Team.key == "money")
        ).one()
        return team_numbers(session, PACK, org.org_id, money, period_of("week", period_day)).issues


def test_an_issue_counts_in_its_visits_week_however_late_it_was_confirmed() -> None:
    org = tiny_org()
    sunday = datetime(2026, 10, 25, 17, 0, tzinfo=UTC)  # Sunday 25 Oct, 22:30 IST
    visit_at(org, sunday, sunday + timedelta(days=2))  # confirmed the next Tuesday

    [numbers] = money_numbers(org, date(2026, 10, 25))

    measures = numbers.measures
    assert (measures.label, measures.shops, measures.insights) == ("new", 1, 1)


def resolve(org: Org_, issue_id: str, when: datetime) -> None:
    with at(when), session_scope() as session:
        found = session.get_one(Issue, issue_id)
        issues.change(
            session,
            org.org_id,
            org.rep_id,
            issue_id,
            found.version,
            status="resolved",
            note="Paid.",
        )


def reopened_by_a_low_one(org: Org_) -> tuple[str, datetime]:
    """High payments at two shops one week, resolved the next, and a low one at the first shop
    the week after, which reopens it: the issue's ID, and the low one's time."""
    with session_scope() as session:
        other = Shop(org_id=org.org_id, name="Another Shop", area="Test Area")
        session.add(other)
        session.flush()
        elsewhere = Org_(org.org_id, org.rep_id, other.id)
    w1, w2, w3 = (datetime(2026, 10, 5 + 7 * n, 6, 0, tzinfo=UTC) for n in range(3))
    high = insight(category="payments", severity="high", suggested_team="money")
    visit_at(org, w1, w1, high)
    visit_at(elsewhere, w1, w1, high)
    [issue] = [n.issue_id for n in money_numbers(org, w1.date())]
    resolve(org, issue, w2)
    visit_at(org, w3, w3)  # low, at the first shop: it reopens
    return issue, w3


def test_a_reopened_issue_counts_only_the_shops_open_since_its_resolve() -> None:
    org = tiny_org()
    _, w3 = reopened_by_a_low_one(org)

    [numbers] = money_numbers(org, w3.date())
    measures = numbers.measures
    assert (measures.label, measures.open_shops, measures.severity, measures.score) == (
        "recurring",
        1,
        "low",
        1 * 1 * (1 + 2),
    )


def test_a_resolved_issue_has_its_last_run_s_severity_as_the_inbox_does() -> None:
    org = tiny_org()
    issue, w3 = reopened_by_a_low_one(org)
    resolve(org, issue, w3 + timedelta(hours=1))

    [numbers] = money_numbers(org, w3.date())

    # Its high insights came before the first resolve.
    measures = numbers.measures
    assert (measures.label, measures.severity, measures.open_shops) == ("resolved", "low", 0)
    with session_scope(read_only=True) as session:
        assert session.get_one(Issue, issue).severity == "low"


def test_a_cause_stands_as_it_did_without_a_team_s_issue_that_came_later() -> None:
    org = tiny_org()
    w1, w2 = datetime(2026, 10, 5, 6, 0, tzinfo=UTC), datetime(2026, 10, 12, 6, 0, tzinfo=UTC)
    visit_at(org, w1, w1)  # a low payment: money's alone
    [issue] = [n.issue_id for n in money_numbers(org, w1.date())]
    resolve(org, issue, w1 + timedelta(days=1))
    # A high one the next week: money's reopens, and the field team gets one of its own.
    visit_at(org, w2, w2, insight(category="payments", severity="high", suggested_team="money"))

    with session_scope(read_only=True) as session:
        found = org_numbers(session, PACK, org.org_id, period_of("week", w1.date()))

    [cause] = found.causes
    assert (cause.measures.label, [(w.team_key, w.status) for w in cause.teams]) == (
        "resolved",
        [("money", "resolved")],
    )
    assert (found.issues_at_end, found.resolved_in_period) == (
        {"open": 0, "in_progress": 0, "overdue": 0},
        1,
    )


# --- the org's numbers, and the checker ------------------------------------------------------


def leadership(world: _k6.World, week: int) -> OrgNumbers:
    with session_scope(read_only=True) as session:
        return org_numbers(session, world.pack, world.org_id, world.week(week))


def test_k6_s_p5_is_one_cause_counted_once_across_its_two_teams(world: _k6.World) -> None:
    written = EXPECTED["written"]["P5"]["W6_org"]
    p5 = _k6.issue_of(world, world.insights["P5"][0], "finance")

    org = leadership(world, 6)

    [cause] = [c for c in org.causes if p5 in {w.issue_id for w in c.teams}]
    assert (cause.measures.insights, cause.measures.shops) == (
        written["insights"],
        written["shops"],
    )
    assert [w.team_key for w in cause.teams] == written["teams"]
    # The org counts each insight once: P5 alone went to two teams in W6.
    teams_sum = 0
    with session_scope(read_only=True) as session:
        for team_id in world.teams.values():
            teams_sum += team_numbers(
                session, world.pack, world.org_id, team_id, world.week(6)
            ).totals.insights
    assert teams_sum - (org.totals.insights - org.totals.triage) == 1


def test_k6_s_summaries_keep_every_rule(world: _k6.World) -> None:
    with session_scope(read_only=True) as session:
        for week in (5, 6):
            period = world.week(week)
            org = org_numbers(session, world.pack, world.org_id, period)
            assert check(session, world.org_id, org) == []
            for team_id in world.teams.values():
                team = team_numbers(session, world.pack, world.org_id, team_id, period)
                assert check(session, world.org_id, team) == []


def test_the_checker_finds_numbers_that_don_t_add_up(world: _k6.World) -> None:
    finance = world.teams["finance"]
    with session_scope(read_only=True) as session:
        team = team_numbers(session, world.pack, world.org_id, finance, world.week(6))
        org = org_numbers(session, world.pack, world.org_id, world.week(6))
        wrong_team = replace(team, totals=replace(team.totals, insights=team.totals.insights + 1))
        summed = replace(org, totals=replace(org.totals, insights=org.totals.insights + 1))
        no_triage = replace(org, totals=replace(org.totals, triage=0))

        assert check(session, world.org_id, wrong_team) == [
            f"the team's totals {wrong_team.totals} aren't a fresh count"
            f" {(team.totals.insights, team.totals.shops)}",
            "the team's insights aren't its issues' insights",
        ]
        assert check(session, world.org_id, summed) == [
            f"the org's totals {summed.totals} aren't a fresh count"
            f" {(org.totals.insights, org.totals.shops)}",
            "the causes and triage don't account for each insight once",
        ]
        assert check(session, world.org_id, no_triage) == [
            "the org's triage isn't a fresh count",
            "the causes and triage don't account for each insight once",
        ]


def test_the_checker_finds_a_route_in_no_issue(world: _k6.World) -> None:
    with session_scope() as session:
        session.execute(
            update(InsightRoute)
            .where(InsightRoute.insight_id == world.insights["P5"][0])
            .values(issue_id=None)
        )

    with session_scope(read_only=True) as session:
        org = org_numbers(session, world.pack, world.org_id, world.week(6))
        problems = check(session, world.org_id, org)

    assert problems[0] == "2 routes of the period's insights in no issue of their team"


def test_the_org_s_categories_competitors_statuses_and_triage() -> None:
    org = tiny_org()
    monday = datetime(2026, 10, 5, 5, 30, tzinfo=UTC)  # 11:00 IST
    rival = insight(
        category="payments", severity="high", suggested_team="money", competitor_mentions=["Rival"]
    )
    visit_at(org, monday, monday, rival)  # money, and field for a high payment
    visit_at(org, monday, monday, insight(category="demand", suggested_team=None))  # triage
    # Rival again, at the same shop, in a finding that also waits in triage.
    again = insight(category="demand", suggested_team=None, competitor_mentions=["Rival"])
    visit_at(org, monday, monday, again)

    with session_scope(read_only=True) as session:
        found = org_numbers(session, PACK, org.org_id, period_of("week", monday.date()))

    [cause] = found.causes
    assert ([w.team_key for w in cause.teams], cause.measures.insights) == (["field", "money"], 1)
    assert found.totals == Totals(insights=3, shops=1, reps=1, visits=3, triage=2)
    assert found.by_category_area == [CategoryArea("payments", "Test Area", 1, 1)]
    assert found.competitors == [CompetitorCount("Rival", 2, 1)]  # 2 insights, 1 shop
    # Due two days after a high one is confirmed: past due by the week's end.
    assert found.issues_at_end == {"open": 2, "in_progress": 0, "overdue": 2}
    assert found.resolved_in_period == 0


def test_k6_s_w3_counts_p3_s_resolve_apart_from_the_issues_still_open(world: _k6.World) -> None:
    org = leadership(world, 3)

    # Finance resolved P3's credit note in W3; the area manager's P3 issue stays open.
    assert org.resolved_in_period == 1
    p3_causes = [
        c
        for c in org.causes
        if _k6.issue_of(world, world.insights["P3"][0], "finance") in {w.issue_id for w in c.teams}
    ]
    assert [(w.team_key, w.status) for w in p3_causes[0].teams] == [
        ("area_manager", "open"),
        ("finance", "resolved"),
    ]
