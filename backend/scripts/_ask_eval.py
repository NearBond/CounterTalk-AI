"""Ask's eval (batch 17d), for run_eval.py --ask: each case's question through ask's own request
and check (R1), on the demo org in the eval database. A module the script uses: it has no main.

The demo org is seeded into the eval database (seed_dev's seed, which leaves an existing one as
it is), and its vocabulary read, as a question to the app would. Everything that can fail
without the network is checked first (the cases, each expected key being one of the org's, the
schema), and the dry run builds every request the same way and prints what would be sent, with
an estimate: nothing is sent. With --yes each question is one call, logged in llm_calls like the
app's; a call that's left to retry (Meta not reached, say) stops the run, and no results are
written. Every exit prints what was paid.

Scored, per case and in total: a case is right when code's check (ask.understood) agrees with
its expected filter, field by field, or, for an off-topic one, refuses it. Each field is scored
only on the cases expected to be understood, as right of those. Its prompt_hash is of the ask
prompt, so a change to it gets a results file of its own. It prints keys and counts, never the
questions.
"""

import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select

from countertalk import ask, llm_calls
from countertalk.config import BACKEND_DIR
from countertalk.db import session_scope
from countertalk.errors import Invalid, RetryableError
from countertalk.llm import pricing
from countertalk.llm.base import CallDetails, Message, Outcome, StructuredLlm
from countertalk.llm.schema import check_schema
from countertalk.models import LlmPurpose, Org, ist_date, utc_now
from countertalk.playbooks import OrgPack, first_problem, load_org_pack, playbook_text, read_yaml
from scripts._paid import Stopped, estimate, show_calls
from scripts._patterns import short_hash
from scripts.seed_dev import DEMO_ORG_NAME, DEMO_PACK, seed

CASES = BACKEND_DIR / "evals" / "ask" / "v1" / "cases.yaml"
FIELDS = ("category", "competitor", "product", "area", "period", "status")
# A dry run's estimate of an answer: eight short fields of JSON, reasoning added on top.
ASK_ANSWER_TOKENS = 80
Packs = dict[tuple[str, int], OrgPack]
Write = Callable[[dict[str, Any], Packs, str], Path]


class Expected(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    category: str | None = None
    competitor: str | None = None
    product: str | None = None
    area: str | None = None
    period: str | None = None
    status: Literal["open", "resolved", "any"] = "any"
    understood: bool = True


class AskCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    name: str
    language: Literal["hi", "en", "mr"]
    question: str
    expected: Expected


class CasesFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: int
    cases: list[AskCase]


def load_cases(path: Path | None = None) -> list[AskCase]:
    path = path or CASES  # read when called, so a test can point it elsewhere
    try:
        found = CasesFile.model_validate(read_yaml(path))
    except ValidationError as error:
        field, problem = first_problem(error)
        raise Invalid("ask_cases", f"{path}: {field}: {problem}") from None
    names = [case.name for case in found.cases]
    if len(set(names)) != len(names) or not names:
        raise Invalid("ask_cases", f"{path}: the cases' names must be there, each once")
    return found.cases


def check(yes: bool, llm: StructuredLlm | None, folders: dict[str, Path], write: Write) -> int:
    """The eval (the module docstring), once run_eval has moved to the eval database."""
    cases = load_cases()
    pack = load_org_pack(*DEMO_PACK, folders["packs"], folders["playbooks"])
    with session_scope() as session:
        seed(session, pack)
    with session_scope(read_only=True) as session:
        org_id = session.scalars(select(Org.id).where(Org.name == DEMO_ORG_NAME)).one()
        vocabulary = ask.vocabulary(session, pack, org_id)
    today = ist_date(utc_now())
    for case in cases:
        _check_expected(case, vocabulary, today)
    schema = ask.schema(vocabulary, today)
    check_schema(schema)  # one that can't be checked is a bug: found before paying
    requests = [(case, ask.messages(pack, vocabulary, case.question, today)) for case in cases]
    print(f"Ask's {len(cases)} cases on {DEMO_ORG_NAME} ({org_id}), today {today}")
    if not yes:
        return _dry_run(requests, schema)
    assert llm is not None
    paid: list[CallDetails] = []
    scored = []
    try:
        for case, messages in requests:
            outcome = llm.complete(messages, "inbox_filter", schema)
            llm_calls.record(None, LlmPurpose.ASK, llm.provider, outcome)
            paid.extend(outcome.calls)
            if isinstance(outcome.error, RetryableError):
                print(f"{case.name}: {outcome.error.code}; no more is sent", file=sys.stderr)
                return 1
            scored.append(_score(case, outcome, vocabulary, today))
            print(_line(scored[-1]))
    except KeyboardInterrupt:
        raise Stopped from None
    finally:
        print("Paid:")
        show_calls(paid)
    results = _report(scored, paid, pack)
    path = write(results, {(pack.id, pack.version): pack}, llm.model)
    print(f"\n{results['right']} of {results['cases']} right. Results: {path}")
    return 0


def _check_expected(case: AskCase, vocabulary: ask.Vocabulary, today: date) -> None:
    """A case whose expected filter names a key the org hasn't can never pass: refused first."""
    offered = {
        "category": vocabulary.categories,
        "competitor": vocabulary.competitors,
        "product": vocabulary.products,
        "area": vocabulary.areas,
        "period": ask.periods(today),
    }
    for field, keys in offered.items():
        value = getattr(case.expected, field)
        if value is not None and value not in keys:
            said = f"{case.name}: its {field} {value!r} isn't one of the demo org's"
            raise Invalid("ask_case", said)


def _dry_run(requests: list[tuple[AskCase, list[Message]]], schema: dict[str, Any]) -> int:
    total = 0.0
    for case, messages in requests:
        found = estimate(messages, schema, ASK_ANSWER_TOKENS)
        total += found.usd
        print(
            f"{case.name}: {found.chars:,} characters, about {found.tokens_in:,} input and"
            f" {found.tokens_out:,} output tokens, ${found.usd:.4f}"
        )
    print(f"Nothing sent. With --yes: {len(requests)} calls, about ${total:.4f}")
    return 0


def _score(
    case: AskCase, outcome: Outcome, vocabulary: ask.Vocabulary, today: date
) -> dict[str, Any]:
    """The case against code's check of the answer, field by field."""
    found: dict[str, Any] = {
        "name": case.name,
        "language": case.language,
        "expected_understood": case.expected.understood,
    }
    if outcome.answer is None:
        code = outcome.error.code if outcome.error is not None else "llm_no_answer"
        if code != "llm_answer_invalid":  # the app fails it (500): its own bucket
            return found | {"error": code, "wrong": ["error"], "right": False}
        refused, answer = True, {}  # never fit: the app says it wasn't understood
    else:
        answer = outcome.answer
        refused = isinstance(ask.understood(answer, vocabulary, today), str)
    found["understood"] = not refused
    if not case.expected.understood:
        found["wrong"] = [] if refused else ["understood"]
    elif refused:
        found["wrong"] = ["understood"]
    else:
        found["wrong"] = [f for f in FIELDS if answer.get(f) != getattr(case.expected, f)]
        found["got"] = {f: answer.get(f) for f in FIELDS}
    found["right"] = found["wrong"] == []
    return found


def _line(scored: dict[str, Any]) -> str:
    if scored["right"]:
        return f"{scored['name']}: right"
    got = scored.get("got", {})
    wrong = ", ".join(f"{f} (got {got.get(f)!r})" if f in got else f for f in scored["wrong"])
    return f"{scored['name']}: wrong: {wrong}"


def _report(scored: list[dict[str, Any]], paid: list[CallDetails], pack: OrgPack) -> dict[str, Any]:
    """The totals: each field right of the cases expected to be understood (one refused gets
    none of its fields right), and the cost."""
    understood = [s for s in scored if s["expected_understood"]]
    by_field = {
        field: {
            "right": sum(1 for s in understood if {field, "understood"}.isdisjoint(s["wrong"])),
            "of": len(understood),
        }
        for field in FIELDS
    }
    return {
        "kind": "ask",
        "prompt_hash": short_hash(playbook_text(pack, "ask_prompt")),
        "cases": len(scored),
        "right": sum(1 for s in scored if s["right"]),
        "by_field": by_field,
        "cost_usd": round(sum(map(pricing.call_cost, paid)), 6),
        "results": scored,
    }
