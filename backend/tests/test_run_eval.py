"""scripts/run_eval.py: the eval cases through the real extract job, scored (batch 13).

The eval runs on the test database here (EVAL_DATABASE pointed at it), so each test starts
clean; a test of the guard shows any other name is refused.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from sqlalchemy import func, select

from countertalk import merges, narrative
from countertalk.db import session_scope
from countertalk.errors import RetryableError
from countertalk.llm import pricing
from countertalk.llm.base import Outcome
from countertalk.models import LlmCall, Org, Summary, Visit, VisitStatus
from countertalk.playbooks import load_org_pack
from countertalk.routing import decide
from scripts import run_eval
from scripts._patterns import short_hash
from scripts.compare_extraction import Expected, Extracted
from tests.conftest import ORG_PACK_FIXTURES, PINNED_SETTINGS, TEST_DATABASE
from tests.test_extraction import VISITED, answer, insight
from tests.test_merges import Answer, ByName
from tests.test_roles import VISIT, FakeLlm, call, tiny

BUSY = Outcome(calls=(call("llm_unavailable"),), error=RetryableError("llm_unavailable", "B"))
ROLES = {"c0:A": "rep", "c0:B": "shopkeeper", "c0:C": "shopkeeper"}
# One K2 insight: the shop asks for two boxes, in the turn at 4.0 s.
KEY = (
    "recording: {name}\ninsights:\n  - {{kind: product_request, category: demand, severity: low,"
    " speaker: shopkeeper, phrase: do dabbe, skus: [crunchy], competitors: [], scheme: null,"
    " teams: [field]}}\n"
)


@pytest.fixture(autouse=True)
def eval_on_the_test_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(run_eval, "EVAL_DATABASE", TEST_DATABASE)
    monkeypatch.setattr(run_eval, "EVALS_DIR", tmp_path / "evals")
    # run_eval sets these itself; monkeypatch puts them back after.
    monkeypatch.setenv("DATABASE_URL", PINNED_SETTINGS["DATABASE_URL"])
    monkeypatch.setenv("DATA_DIR", str(tmp_path))


def a_case(cases: Path, name: str, **changes: Any) -> None:
    folder = cases / name
    folder.mkdir(parents=True)
    file = {
        "version": 1,
        "recording": name,
        "org_pack": {"id": "tiny-demo", "version": 1},
        "visited_at": VISITED.isoformat(),
        "transcript": {"provider": "meta", "model": "test", "language_code": "hi,en"},
        "roles": {
            "playbook": {"id": "fmcg-kirana-visit", "version": 1},
            "speakers": {label: {"role": role, "confidence": 0.9} for label, role in ROLES.items()},
        },
        "turns": [
            {"turn_id": n, "label": label, "start_ms": at, "end_ms": at + 900, "text": words}
            for n, (label, at, words) in enumerate(VISIT)
        ],
    } | changes
    (folder / "turns.json").write_text(json.dumps(file, ensure_ascii=False), encoding="utf-8")
    script = {
        "turns": [{"role": "shopkeeper", "text": "do dabbe", "start_ms": 4_000, "end_ms": 4_900}]
    }
    (folder / "script_turns.json").write_text(json.dumps(script), encoding="utf-8")
    (folder / "expected.yaml").write_text(KEY.format(name=name), encoding="utf-8")


def eval_(tmp_path: Path, *args: str, llm: FakeLlm | None = None) -> int:
    return run_eval.main(
        [*args, "--cases", str(tmp_path / "cases"), "--root", str(ORG_PACK_FIXTURES)], llm=llm
    )


def calls_made() -> int:
    with session_scope(read_only=True) as session:
        return session.scalar(select(func.count()).select_from(LlmCall)) or 0


def test_a_dry_run_builds_each_request_and_sends_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a_case(tmp_path / "cases", "rp98")
    a_case(tmp_path / "cases", "rp99")

    assert eval_(tmp_path) == 0

    out = capsys.readouterr().out.splitlines()
    assert [line.split(":")[0] for line in out] == ["rp98", "rp99", "2 cases"]
    assert out[0].startswith("rp98: 4 turns, ")
    assert out[2].endswith("(an estimate: --yes sends them)")
    assert calls_made() == 0
    assert not (tmp_path / "evals" / "results").exists()


def test_each_case_runs_the_real_extract_job_and_is_scored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a_case(tmp_path / "cases", "rp99")
    # One that matches K2's, and one it hasn't got: the shopkeeper's "Haan, yeh lo." at 70 s.
    thanks = insight(kind="praise", category="praise", evidence=[{"turn_id": 3, "quote": "Haan"}])
    order = [
        {"name": "Crunchy", "quantity": 2, "unit": "dabbe"},
        {"name": "Zingo", "quantity": None, "unit": None},
    ]
    found = answer(insight(sku_mentions=["Crunchy"]), thanks, order_items=order)
    llm = FakeLlm(found)  # roles saved: extraction

    assert eval_(tmp_path, "--yes", llm=llm) == 0

    assert [name for _, name, _ in llm.asked] == ["extraction"]
    with session_scope(read_only=True) as session:
        assert session.scalars(select(Visit.status)).all() == [VisitStatus.REVIEW]
    out = capsys.readouterr().out
    # compare_extraction's own table, missed and invented included.
    assert "\nrp99, job done: 1 matched, 0 missed, 1 invented, of 2 extracted\n" in out
    assert "\n  INVENTED praise/praise/low: The shop wants two boxes.\n" in out
    assert "  product_request: recall 1/1 (100%) of K2 product_request (optional aside)," in out
    for way in ("table", "ai", "rule"):
        assert f"  routing, {way}: " in out
    # The order, as said and as linked: rp05's is all order, and no insight.
    lines = out.splitlines()
    at = lines.index("  order items: 2 (linked 1, to_review 1, other 0)")
    assert lines[at : at + 5] == [
        "  order items: 2 (linked 1, to_review 1, other 0)",
        "    Crunchy: 2 dabbe -> crunchy",
        "    Zingo: no quantity said -> not in the catalog",
        "",
        "Totals, 1 cases:",
    ]
    [results] = (tmp_path / "evals" / "results").iterdir()
    found = json.loads(results.read_text(encoding="utf-8"))
    assert results.name == f"{found['date']}-{found['prompt_hash']}.json"
    case = found["cases"]["rp99"]
    assert (case["job"], case["required"], case["matched_required"]) == ("done", 1, 1)
    assert case["kinds"] == {
        "praise": {"both": 0, "required": 0, "extracted": 1},
        "product_request": {"both": 1, "required": 1, "extracted": 1},
    }
    assert (case["missed"], case["invented"]) == (
        [],
        [
            {
                "kind": "praise",
                "category": "praise",
                "severity": "low",
                "summary": "The shop wants two boxes.",
            }
        ],
    )
    # demand has no table team: the AI's field is the rule's, and K2's.
    assert case["routing"] == {
        "matched_verified": 1,
        "matched_unverified": 0,
        "table": 0,
        "ai": 1,
        "rule": 1,
    }
    assert case["links"]["sku"] == {"agree": 1}
    assert case["order_items"] == {"items": 2, "linked": 1, "to_review": 1, "other": 0}


def test_a_case_left_to_retry_stops_the_run_and_what_was_paid_is_shown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a_case(tmp_path / "cases", "rp98")
    a_case(tmp_path / "cases", "rp99")
    llm = FakeLlm(BUSY, answer())

    assert eval_(tmp_path, "--yes", llm=llm) == 1

    assert len(llm.asked) == 1  # rp99 was never sent
    out, err = capsys.readouterr()
    lines = out.splitlines()
    cost = f"${pricing.call_cost(BUSY.calls[0]):.4f}"
    assert (lines[-3], lines[-2].startswith("call 1: extraction llm_unavailable,"), lines[-1]) == (
        "Paid:",
        True,
        f"cost: {cost}",
    )
    assert err == "rp98: RetryableError: llm_unavailable: B; no more is sent\n"
    assert not (tmp_path / "evals" / "results").exists()


def test_each_run_is_in_an_org_of_its_own(tmp_path: Path) -> None:
    a_case(tmp_path / "cases", "rp99")

    eval_(tmp_path)
    eval_(tmp_path)

    with session_scope(read_only=True) as session:
        assert session.scalar(select(func.count()).select_from(Org)) == 2
        assert session.scalar(select(func.count(func.distinct(Visit.org_id)))) == 2


def test_only_the_eval_database_is_written(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_eval, "EVAL_DATABASE", "countertalk_eval")

    with pytest.raises(run_eval.Refused) as refused:
        run_eval.check_eval_database()  # the engine is still on the test database

    assert str(refused.value) == (
        f"refusing: '{TEST_DATABASE}' isn't the eval database (countertalk_eval)"
    )


def test_the_eval_database_is_dev_s_server_with_the_eval_s_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(run_eval, "EVAL_DATABASE", "countertalk_eval")

    run_eval.use_eval_database()

    from countertalk.config import get_settings

    settings = get_settings()
    assert settings.DATABASE_URL.rsplit("/", 1) == [
        PINNED_SETTINGS["DATABASE_URL"].rsplit("/", 1)[0],
        "countertalk_eval",
    ]
    assert settings.DATA_DIR == tmp_path / "evals" / "work"


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"version": 2}, "rp99: turns.json version: Input should be 1"),
        (
            {"turns": []},
            "rp99: turns.json turns: List should have at least 1 item after validation, not 0",
        ),
        (
            {"visited_at": "2026-11-01T06:00:00"},
            "rp99: turns.json's visited_at isn't a time with its zone",
        ),
        ({"recording": "rp98"}, "rp99: turns.json is rp98's"),
        (
            {"roles": {"playbook": {"id": "fmcg-kirana-visit", "version": 1}, "speakers": {}}},
            "rp99: turns.json's roles aren't its turns' labels",
        ),
        (
            {
                "roles": {
                    "playbook": {"id": "fmcg-kirana-visit", "version": 9},
                    "speakers": {
                        label: {"role": role, "confidence": 0.9} for label, role in ROLES.items()
                    },
                }
            },
            "rp99: its roles are fmcg-kirana-visit v9's, not its pack's fmcg-kirana-visit v1",
        ),
    ],
    ids=["version", "no-turns", "no-zone", "another-recording", "roles-unsaved", "roles-other"],
)
def test_a_case_is_read_strictly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], change: dict[str, Any], why: str
) -> None:
    a_case(tmp_path / "cases", "rp99", **change)

    assert eval_(tmp_path) == 1

    assert capsys.readouterr().err == f"{why}\n"


def test_another_recording_s_answer_key_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a_case(tmp_path / "cases", "rp99")
    key = tmp_path / "cases" / "rp99" / "expected.yaml"
    key.write_text(KEY.format(name="rp98"), encoding="utf-8")

    assert eval_(tmp_path) == 1

    assert capsys.readouterr().err == (
        "rp99: script_turns.json or expected.yaml: it's rp98's answer key\n"
    )


def test_without_yes_nothing_is_sent_even_with_a_model(tmp_path: Path) -> None:
    a_case(tmp_path / "cases", "rp99")
    llm = FakeLlm(answer())

    assert eval_(tmp_path, llm=llm) == 0

    assert (llm.asked, calls_made()) == ([], 0)


def test_results_that_cant_be_written_are_one_line_after_the_paid_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    a_case(tmp_path / "cases", "rp99")

    def full(path: Path, body: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(run_eval.storage, "write_file", full)

    assert eval_(tmp_path, "--yes", llm=FakeLlm(answer())) == 1

    out, err = capsys.readouterr()
    assert "Paid:\n" in out
    assert err.startswith("results not written: ") and err.endswith(" (OSError)\n")


def got(category: str, severity: str, team: str | None) -> Extracted:
    return Extracted(0, "problem", category, severity, team, None, "s", ((0, 1),), ("exact",), True)


@pytest.mark.parametrize(
    ("category", "severity", "ai", "ways"),
    [
        # The table's money; the AI says field. A high payment also goes to field (override).
        ("payments", "low", "field", {"table": {"money"}, "ai": {"field"}, "rule": {"money"}}),
        (
            "payments",
            "high",
            "money",
            {"table": {"money", "field"}, "ai": {"money", "field"}, "rule": {"money", "field"}},
        ),
        # No table team: the table alone sends it to triage.
        ("demand", "low", "field", {"table": set(), "ai": {"field"}, "rule": {"field"}}),
    ],
)
def test_routing_three_ways(
    category: str, severity: str, ai: str | None, ways: dict[str, set[str]]
) -> None:
    pack = tiny()
    table = run_eval.routing.table_of(pack)

    assert run_eval.routed_ways(got(category, severity, ai), table, list(pack.file.teams)) == ways
    # The rule is decide() itself.
    assert ways["rule"] == {
        c.team for c in decide(category, severity, ai, table, ["money", "field"]).routes
    }


def test_precision_and_recall_count_a_kind_only_when_both_sides_are_it() -> None:
    def want(number: int, kind: str, optional: bool = False) -> Expected:
        return Expected(number, kind, "supply", "low", ("field",), None, optional, "p", (0, 1))

    def extracted(seq: int, kind: str) -> Extracted:
        return Extracted(
            seq, kind, "supply", "low", "field", None, "s", ((0, 1),), ("exact",), True
        )

    expected = [want(1, "problem"), want(2, "request"), want(3, "praise", optional=True)]
    found = [extracted(0, "problem"), extracted(1, "problem"), extracted(2, "praise")]
    pairs = [(expected[0], found[0]), (expected[1], found[1]), (expected[2], found[2])]
    score = run_eval.Score()

    score.add(expected, found, pairs, tiny(), 0.01)

    assert score.as_json()["kinds"] == {
        "praise": {"both": 0, "required": 0, "extracted": 0},  # its match was optional
        "problem": {"both": 1, "required": 1, "extracted": 2},
        "request": {"both": 0, "required": 1, "extracted": 0},
    }


def test_an_unverified_insight_is_left_out_of_the_routing_s_whole() -> None:
    want = Expected(1, "problem", "supply", "low", ("field",), None, False, "p", (0, 1))
    unverified = Extracted(
        0, "problem", "supply", "low", None, None, "s", ((0, 1),), ("none",), False
    )
    score = run_eval.Score()

    score.add([want], [unverified], [(want, unverified)], tiny(), 0.0)

    assert score.as_json()["routing"] == {
        "matched_verified": 0,
        "matched_unverified": 1,
        "table": 0,
        "ai": 0,
        "rule": 0,
    }
    assert score.lines()[-5:-1] == [
        "routing, table: 0/0 of matched and verified",
        "routing, ai: 0/0 of matched and verified",
        "routing, rule: 0/0 of matched and verified",
        "routing: 1 matched but unverified, not counted",
    ]


# --- the K6 pattern check (batch 15c) --------------------------------------------------------

P1_TITLE = "Schemes and promotions · Kurkure Diwali payout · Riverbend Traders (example)"
E_TITLE = "Schemes and promotions · Riverbend Traders (example)"


def the_pair(payload: dict[str, Any]) -> dict[str, Any]:
    """Finance's schemes: E's issue into P1's, as K6 expects; anything else, nothing."""
    ids = {issue["title"]: issue["id"] for issue in payload["issues"]}
    if P1_TITLE in ids and E_TITLE in ids:
        return {
            "suggestions": [{"into": ids[P1_TITLE], "merge": [ids[E_TITLE]], "reason": "Same."}]
        }
    return {"suggestions": []}


def one_sentence(payload: dict[str, Any]) -> dict[str, Any]:
    """An overview sentence citing the first quote it was given, with no number."""
    first = payload["issues"][0]["quotes"][0]["insight_id"]
    return {"overview": [{"text": "Shopkeepers raised these.", "cites": [first]}], "issues": []}


def test_the_pattern_check_s_dry_run_prices_every_team_s_w6_and_sends_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_eval.main(["--patterns"]) == 0

    out = capsys.readouterr().out.splitlines()
    assert out[0].endswith("every team's summary of week 2026-10-26 to 2026-11-01 (W6)")
    assert [line.split(":")[0] for line in out[1:]] == [
        "area_manager",
        "distribution",
        "finance",
        "sales_ops",
        "trade_marketing",
        "in all",
    ]
    assert out[-1].endswith("(an estimate: --patterns --yes sends it)")
    with session_scope(read_only=True) as session:
        assert session.scalar(select(func.count()).select_from(Summary)) == 0
    assert calls_made() == 0


def test_the_pattern_check_scores_the_merge_and_the_narratives(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    llm = ByName(merge_suggestions=the_pair, summary_narrative=one_sentence)

    assert run_eval.main(["--patterns", "--yes"], llm=llm) == 0

    [results] = (tmp_path / "evals" / "results").glob("*.json")
    found = json.loads(results.read_text(encoding="utf-8"))
    assert found["kind"] == "patterns"
    assert results.name == f"{found['date']}-{found['prompt_hash']}-patterns.json"
    assert found["prompt_hash"] == short_hash(merges.PROMPT + narrative.PROMPT)
    assert found["prompts"] == {
        "merge": short_hash(merges.PROMPT),
        "narrative": short_hash(narrative.PROMPT),
    }
    assert {
        k: found["merge"][k] for k in ("suggested", "right", "precision", "recall", "keeps_p1")
    } == {
        "suggested": 1,
        "right": 1,
        "precision": 1.0,
        "recall": 1.0,
        "keeps_p1": True,
    }
    asked = sum(1 for name, _ in llm.asked if name == "summary_narrative")
    assert (found["narrative"]["kept"], found["narrative"]["drop_rate"]) == (asked, 0.0)
    assert set(found["statuses"].values()) == {"ready"}
    assert found["cost_usd"]["total"] == pytest.approx(calls_made() * pricing.cost(900, 0, 60))
    assert "Paid:" in capsys.readouterr().out


def test_a_pattern_job_left_to_retry_stops_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    busy = Outcome(
        calls=(call("llm_busy"),), error=RetryableError("llm_busy", "Muse Spark is busy")
    )
    llm = ByName(merge_suggestions={"suggestions": []}, summary_narrative=busy)

    assert run_eval.main(["--patterns", "--yes"], llm=llm) == 1

    narratives = [name for name, _ in llm.asked if name == "summary_narrative"]
    assert len(narratives) == 1  # the first team's, then no more
    err = capsys.readouterr().err
    assert err == "area_manager: RetryableError: llm_busy: Muse Spark is busy; no more is sent\n"
    assert not (tmp_path / "evals" / "results").exists()  # it would score what never ran


def finance_fails(payload: dict[str, Any]) -> Answer:
    """Finance's schemes call fails; the rest suggest nothing."""
    if P1_TITLE in {issue["title"] for issue in payload["issues"]}:
        return Outcome(calls=(call("llm_busy"),), error=RetryableError("llm_busy", "busy"))
    return {"suggestions": []}


def test_a_merge_step_with_no_full_answer_is_its_own_bucket_not_a_miss(tmp_path: Path) -> None:
    llm = ByName(merge_suggestions=finance_fails, summary_narrative=one_sentence)

    assert run_eval.main(["--patterns", "--yes"], llm=llm) == 0

    [results] = (tmp_path / "evals" / "results").glob("*.json")
    merge = json.loads(results.read_text(encoding="utf-8"))["merge"]
    assert merge["unanswered"] == {"finance": "1 of 2 calls failed"}
    assert (merge["recall"], merge["keeps_p1"], merge["precision"]) == (None, None, None)


def test_a_cases_run_and_a_patterns_run_on_one_day_never_share_a_file() -> None:
    packs = {("pepsico-india-demo", 1): load_org_pack("pepsico-india-demo", 1)}
    noon = datetime(2026, 11, 2, 6, 30, tzinfo=UTC)

    with mock.patch.object(run_eval, "utc_now", lambda: noon):  # one day for both, and the names
        patterns = run_eval.write_results({"kind": "patterns"}, packs, "fake-spark-1")
        cases = run_eval.write_results({"cases": {}}, packs, "fake-spark-1")

    stem = f"2026-11-02-{run_eval.prompt_hash(packs)}"
    assert (cases.name, patterns.name) == (f"{stem}.json", f"{stem}-patterns.json")


def test_patterns_takes_no_cases(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_eval.main(["--patterns", "rp01"]) == 1

    assert capsys.readouterr().err == "--patterns runs K6, not cases: no case names or --cases\n"
