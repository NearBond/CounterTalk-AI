"""Runs the eval cases (batch 13): the real extract job (extraction, quotes checked, linking and
routing) on each case's stored turns and saved roles, against its own database. A dev tool;
paid runs are the planner's.

    uv run python scripts/run_eval.py [rp01 rp02 …] [--yes] [--cases <folder>]
        [--root <folder>] [--playbooks <folder>]

Only the database countertalk_eval: DATABASE_URL's server, with the name swapped before
anything connects, created and migrated if need be. A guard refuses any other before a write,
so the dev database is never touched. Its files go to data/evals/work. Each run imports each
case's org pack into a new org of its own, so runs never mix.

Without --yes it builds each case's visit and request the same way and prints what would be
sent, with an estimate: nothing is sent. With --yes it runs each case's extract job the
worker's way. A case whose job is left to retry (Meta not reached, say) stops the run: no more
is sent. Every exit prints what was paid. A finished run writes data/evals/results/
{date}-{prompt_hash}.json; prompt_hash is the first 12 hex digits of the playbook part's
SHA-256 (prompt.md and the codebook), so a prompt change gets a file of its own.

Scored with compare_extraction's own matching and counts (R1), and, per case and in total:
- precision and recall per kind: a pair counts for kind K only if both sides are K. Recall is
  of K2's required insights of kind K; precision of the extracted of kind K, optional matches
  aside;
- routing three ways, from each matched insight's category, severity and AI team, against K2's
  teams in any order: the table alone (decide() with no AI team), the AI alone (decide() on the
  table with every category's team taken out, the overrides kept), and the rule (decide());
- the cost of each case's calls.
It prints quotes' matches and counts, never the words.
"""

import argparse
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from alembic import command
from alembic.config import Config
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from countertalk import catalog, entities, extraction, routing, storage
from countertalk.config import BACKEND_DIR, get_settings
from countertalk.db import SETUP_ERRORS, get_engine, session_scope, setup_trouble
from countertalk.errors import DomainError, describe
from countertalk.llm.base import StructuredLlm
from countertalk.logs import configure_logging
from countertalk.models import (
    AliasReview,
    CatalogItem,
    CatalogKind,
    JobState,
    OrderItem,
    Org,
    Role,
    Shop,
    SpeakerRole,
    User,
    Visit,
    VisitClient,
    VisitKind,
    VisitStatus,
    ist_date,
    new_id,
    utc_now,
)
from countertalk.playbooks import (
    ORG_PACKS_DIR,
    PLAYBOOKS_DIR,
    OrgPack,
    Route,
    first_problem,
    load_org_pack,
)
from countertalk.reporting import buckets, share
from countertalk.transcription import add_transcript
from scripts._paid import Paid, Stopped, estimate, model_llm, paid_run, run_extract_job
from scripts.compare_extraction import (
    Expected,
    Extracted,
    Result,
    Review,
    Totals,
    load_expected,
    load_extracted,
    match_insights,
    misses,
)
from scripts.compare_extraction import report as compared
from scripts.compare_transcript import BadReference, load_reference
from scripts.extract_visit import ANSWER_TOKENS

EVAL_DATABASE = "countertalk_eval"
CASES = BACKEND_DIR / "evals" / "fmcg-kirana-visit" / "v1"
EVALS_DIR = BACKEND_DIR / "data" / "evals"
WAYS = ("table", "ai", "rule")
ORDER_WAYS = ("linked", "to_review", "other")

Packs = dict[tuple[str, int], OrgPack]


class Refused(Exception):
    """Why the run can't go on, in one line."""


# --- the eval database -----------------------------------------------------------------------


def use_eval_database() -> None:
    """DATABASE_URL's server with the eval database's name, and the eval's own data folder,
    before anything connects."""
    url = make_url(get_settings().DATABASE_URL).set(database=EVAL_DATABASE)
    os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
    os.environ["DATA_DIR"] = str(EVALS_DIR / "work")
    get_settings.cache_clear()


def check_eval_database() -> None:
    """The guard before any write: the engine's database must be the eval's."""
    name = get_engine().url.database
    if name != EVAL_DATABASE:
        raise Refused(f"refusing: {name!r} isn't the eval database ({EVAL_DATABASE})")


def prepare_database() -> None:
    check_eval_database()
    server = create_engine(get_engine().url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with server.connect() as conn:
            found = text("SELECT 1 FROM pg_database WHERE datname = :name")
            if conn.scalar(found, {"name": EVAL_DATABASE}) is None:
                conn.execute(text(f'CREATE DATABASE "{EVAL_DATABASE}"'))
    finally:
        server.dispose()
    command.upgrade(Config(toml_file=str(BACKEND_DIR / "pyproject.toml")), "head")


# --- the cases -------------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class CasePack(_Strict):
    id: str
    version: int = Field(ge=1)


class CaseTranscript(_Strict):
    provider: str
    model: str
    language_code: str | None


class CaseSpeaker(_Strict):
    role: str
    confidence: Annotated[float, Field(ge=0, le=1, strict=False)]


class CaseRoles(_Strict):
    playbook: CasePack
    speakers: dict[str, CaseSpeaker]


class CaseTurn(_Strict):
    turn_id: int = Field(ge=0)
    label: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str


class CaseFile(_Strict):
    """An eval case's turns.json (scripts/export_eval_case.py), read strictly (R4)."""

    version: Literal[1]
    recording: str
    org_pack: CasePack
    visited_at: str
    transcript: CaseTranscript
    roles: CaseRoles
    turns: list[CaseTurn] = Field(min_length=1)


@dataclass(frozen=True)
class Case:
    name: str
    file: CaseFile
    visited_at: datetime
    expected: list[Expected]


def load_case(folder: Path) -> Case:
    name = folder.name
    try:
        file = CaseFile.model_validate_json((folder / "turns.json").read_bytes())
    except OSError:
        raise Refused(f"{name}: turns.json can't be read") from None
    except ValidationError as error:
        where, what = first_problem(error)
        raise Refused(f"{name}: turns.json {where}: {what}") from None
    if file.recording != name:
        raise Refused(f"{name}: turns.json is {file.recording}'s")
    try:
        visited_at = datetime.fromisoformat(file.visited_at)
    except ValueError:
        visited_at = None
    if visited_at is None or visited_at.tzinfo is None:
        raise Refused(f"{name}: turns.json's visited_at isn't a time with its zone")
    # Every label's role saved, or the paid run would pay for roles the estimate leaves out.
    labels = {turn.label for turn in file.turns}
    if set(file.roles.speakers) != labels:
        raise Refused(f"{name}: turns.json's roles aren't its turns' labels")
    try:
        script = load_reference(folder / "script_turns.json", 0)
        expected = load_expected(folder / "expected.yaml", script, recording=name)
    except BadReference as why:
        raise Refused(f"{name}: script_turns.json or expected.yaml: {why}") from None
    return Case(name, file, visited_at, expected)


def load_cases(folder: Path, names: Sequence[str]) -> list[Case]:
    if not folder.is_dir():
        raise Refused(f"no cases folder at {folder}")
    found = sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith("."))
    if names:
        missing = sorted(set(names) - {p.name for p in found})
        if missing:
            raise Refused(f"no such case: {', '.join(missing)}")
        found = [p for p in found if p.name in names]
    if not found:
        raise Refused(f"no cases in {folder}")
    return [load_case(path) for path in found]


# --- each case's visit -----------------------------------------------------------------------


def org_for(pack: OrgPack, stamp: str) -> tuple[str, str, str]:
    """A new org on the pack, with a rep and a shop: (org, rep, shop)."""
    with session_scope() as session:
        org = Org(name=f"Eval {stamp} {new_id()[:8]} {pack.id} v{pack.version}")
        session.add(org)
        session.flush()
        rep = User(
            org_id=org.id, email=f"eval-{org.id}@example.com", name="Eval rep", role=Role.REP
        )
        shop = Shop(org_id=org.id, name="Eval shop", area="Eval")
        session.add_all([rep, shop])
        session.flush()
        catalog.import_org_pack(session, org.id, pack)
        return org.id, rep.id, shop.id


def check_roles(cases: Sequence[Case], packs: Packs) -> None:
    """Each case's saved roles are its pack's playbook's, so extraction reuses them (R8)."""
    for case in cases:
        playbook = pack_of(case, packs).playbook
        saved = case.file.roles.playbook
        if (saved.id, saved.version) != (playbook.id, playbook.version):
            raise Refused(
                f"{case.name}: its roles are {saved.id} v{saved.version}'s, not its pack's"
                f" {playbook.id} v{playbook.version}"
            )


def visit_for(case: Case, org: tuple[str, str, str]) -> str:
    """The case's visit, transcribed, with its turns and saved roles: what extraction reads."""
    org_id, rep_id, shop_id = org
    file = case.file
    with session_scope() as session:
        visit = Visit(
            org_id=org_id,
            shop_id=shop_id,
            rep_id=rep_id,
            kind=VisitKind.CONVERSATION,
            client=VisitClient.UPLOAD,
            status=VisitStatus.TRANSCRIBED,
            source_ext="m4a",
            duration_ms=max(t.end_ms for t in file.turns),
            completed_at=case.visited_at,
            created_at=case.visited_at,
        )
        session.add(visit)
        session.flush()
        turns = [
            {
                "turn_id": t.turn_id,
                "speaker_label": t.label,
                "start_ms": t.start_ms,
                "end_ms": t.end_ms,
                "text": t.text,
            }
            for t in file.turns
        ]
        made = file.transcript
        languages = made.language_code or ""
        transcript_id = add_transcript(session, visit, made.provider, made.model, languages, turns)
        session.add_all(
            SpeakerRole(
                transcript_id=transcript_id,
                speaker_label=label,
                role=speaker.role,
                confidence=speaker.confidence,
                playbook_id=file.roles.playbook.id,
                playbook_version=file.roles.playbook.version,
            )
            for label, speaker in file.roles.speakers.items()
        )
        return visit.id


def prepare_visits(cases: Sequence[Case], packs: Packs) -> dict[str, str]:
    """Each case's visit, in a new org per pack: {case: visit}."""
    check_eval_database()
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    orgs = {key: org_for(pack, stamp) for key, pack in packs.items()}
    return {
        case.name: visit_for(case, orgs[(case.file.org_pack.id, case.file.org_pack.version)])
        for case in cases
    }


# --- scoring ---------------------------------------------------------------------------------


def without_teams(table: routing.Table) -> routing.Table:
    """The table with every category's team taken out and its override kept: the AI alone."""
    routes = {c: Route(c, None, r.override) for c, r in table.routes.items()}
    return routing.Table(routes, table.severities)


def routed_ways(
    got: Extracted, table: routing.Table, team_keys: Sequence[str]
) -> dict[str, set[str]]:
    """The teams each way would give the insight: the table alone, the AI alone, the rule."""

    def teams(ai: str | None, used: routing.Table) -> set[str]:
        decided = routing.decide(got.category, got.severity, ai, used, team_keys)
        return {choice.team for choice in decided.routes}

    return {
        "table": teams(None, table),
        "ai": teams(got.team, without_teams(table)),
        "rule": teams(got.team, table),
    }


@dataclass
class Score:
    """A case's, or the run's, counts beyond compare_extraction's Totals."""

    # kind: [pairs of that kind on both sides, K2's required of it, extracted of it]
    kinds: dict[str, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0, 0]))
    ways: Counter[str] = field(default_factory=Counter)
    # Matched and verified: the routing's whole. An unverified insight is never routed (D9).
    routed: int = 0
    unverified: int = 0
    usd: float = 0.0

    def add(
        self,
        expected: Sequence[Expected],
        extracted: Sequence[Extracted],
        pairs: Sequence[tuple[Expected, Extracted]],
        pack: OrgPack,
        usd: float,
    ) -> None:
        table, team_keys = routing.table_of(pack), list(pack.file.teams)
        optional = {got.seq for want, got in pairs if want.optional}
        for want in expected:
            self.kinds[want.kind][1] += not want.optional
        for got in extracted:
            self.kinds[got.kind][2] += got.seq not in optional
        for want, got in pairs:
            if want.kind == got.kind and not want.optional:
                self.kinds[want.kind][0] += 1
            if not got.verified:
                self.unverified += 1
                continue
            for way, teams in routed_ways(got, table, team_keys).items():
                self.ways[way] += teams == set(want.teams)
            self.routed += 1
        self.usd += usd

    def lines(self) -> list[str]:
        found = [
            f"{kind}: recall {share(both, required, f'K2 {kind} (optional aside)')},"
            f" precision {share(both, extracted, f'extracted {kind} (optional aside)')}"
            for kind, (both, required, extracted) in sorted(self.kinds.items())
        ]
        whole = "matched and verified"
        found += [f"routing, {way}: {share(self.ways[way], self.routed, whole)}" for way in WAYS]
        found.append(f"routing: {self.unverified} matched but unverified, not counted")
        return [*found, f"cost: ${self.usd:.4f}"]

    def as_json(self) -> dict[str, Any]:
        return {
            "kinds": {
                kind: dict(zip(("both", "required", "extracted"), counts, strict=True))
                for kind, counts in sorted(self.kinds.items())
            },
            "routing": {
                "matched_verified": self.routed,
                "matched_unverified": self.unverified,
                **{way: self.ways[way] for way in WAYS},
            },
            "usd": round(self.usd, 6),
        }


def totals_json(totals: Totals) -> dict[str, Any]:
    """compare_extraction's counts, as numbers: each share's part and whole."""
    return {
        "required": totals.required,
        "matched_required": totals.matched_required,
        "matched_optional": totals.matched_optional,
        "extracted": totals.extracted,
        "matched": totals.pairs,
        "category_agrees": totals.categories,
        "severity_agrees": totals.severities,
        "team_suggestion_agrees": totals.teams,
        "quotes": dict(totals.matches),
        "verified": totals.verified,
        "invented_where_none_expected": totals.invented_where_none_expected,
        "links": {kind: dict(counts) for kind, counts in totals.scores.links.items()},
        "routed_teams": dict(totals.scores.routing),
        "alias_reviews": dict(totals.reviews),
    }


@dataclass
class Scored:
    """One case, as compared and counted."""

    extracted: list[Extracted]
    reviews: list[Review]
    result: Result
    totals: Totals
    score: Score


@dataclass
class Run:
    """What's scored, per case and for the run."""

    cases: dict[str, Scored] = field(default_factory=dict)
    totals: Totals = field(default_factory=Totals)
    score: Score = field(default_factory=Score)

    def add(self, case: Case, visit_id: str, pack: OrgPack, usd: float) -> None:
        loaded = load_extracted(visit_id)
        if loaded is None:
            raise Refused(f"{case.name}: its visit has no transcript")
        extracted, reviews = loaded
        result = match_insights(case.expected, extracted)
        scored = Scored(extracted, reviews, result, Totals(), Score())
        for totals, score in ((scored.totals, scored.score), (self.totals, self.score)):
            totals.add(case.expected, extracted, result, reviews)
            score.add(case.expected, extracted, result.pairs, pack, usd)
        self.cases[case.name] = scored


# --- running ---------------------------------------------------------------------------------


def pack_of(case: Case, packs: Packs) -> OrgPack:
    return packs[(case.file.org_pack.id, case.file.org_pack.version)]


def prompt_hash(packs: Packs) -> str:
    """Of the playbook part every case's request starts with; one, as every case's pack uses
    the same playbook in practice. With several, of them all in order."""
    parts = sorted({extraction.playbook_part(pack) for pack in packs.values()})
    return hashlib.sha256("".join(parts).encode()).hexdigest()[:12]


def dry_run(cases: Sequence[Case], visits: dict[str, str], packs: Packs) -> int:
    """The same requests --yes would send, built the same way, and an estimate."""
    total = 0.0
    for case in cases:
        speakers = {label: s.role for label, s in case.file.roles.speakers.items()}
        request = extraction.request_for(visits[case.name], pack_of(case, packs), speakers)
        found = estimate(request.messages, request.schema, ANSWER_TOKENS)
        total += found.usd
        print(
            f"{case.name}: {request.turns} turns, {found.chars:,} characters; about"
            f" {found.tokens_in:,} input and {found.tokens_out:,} output tokens (reasoning"
            f" counted), ${found.usd:.4f}"
        )
    print(f"{len(cases)} cases: about ${total:.4f} (an estimate: --yes sends them)")
    return 0


def run(
    cases: Sequence[Case],
    visits: dict[str, str],
    llm: StructuredLlm,
    folders: dict[str, Path],
) -> dict[str, str] | int:
    """Each case's extract job, the worker's way: each case's final job state, or an exit code
    once one is left to retry (no more is sent) or the run is stopped. Inside paid_run, which
    prints what was paid on every way out (R8)."""
    states: dict[str, str] = {}
    for case in cases:
        with session_scope(read_only=True) as session:
            visit = session.get_one(Visit, visits[case.name])
        done = run_extract_job(visit, False, llm, folders)
        if done is None:
            print(f"{case.name}: its visit had no job to run", file=sys.stderr)
            return 1
        if done.stopped:
            print(f"{case.name}: stopped, so no more is sent", file=sys.stderr)
            return 130
        if done.state == JobState.QUEUED:
            print(f"{case.name}: {done.last_error}; no more is sent", file=sys.stderr)
            return 1
        if done.state != JobState.DONE:
            print(f"{case.name}: failed: {done.last_error}", file=sys.stderr)
        states[case.name] = done.state
    return states


def report(
    cases: Sequence[Case], visits: dict[str, str], states: dict[str, str], packs: Packs, paid: Paid
) -> dict[str, Any]:
    scored = Run()
    for case in cases:
        visit_id = visits[case.name]
        scored.add(case, visit_id, pack_of(case, packs), paid.usd(visit_id))
    found: dict[str, Any] = {}
    ordered = Counter[str]()
    for name, case in scored.cases.items():
        # compare_extraction's own table (R1): each pair, and each insight missed or invented.
        compared(f"{name}, job {states[name]}", case.result, case.extracted, case.reviews)
        order, ways = order_of(visits[name])
        lines = [*case.totals.headline(), *case.score.lines(), *order_lines(order, ways)]
        print("\n".join(f"  {line}" for line in lines))
        counts = {"items": len(order), **{way: ways[way] for way in ORDER_WAYS}}
        ordered.update(counts)
        found[name] = {
            "job": states[name],
            **totals_json(case.totals),
            **case.score.as_json(),
            **misses(case.result),
            "order_items": counts,
        }
    lines = [*scored.totals.lines(), *scored.score.lines()]
    print(f"\nTotals, {len(cases)} cases:")
    print("\n".join(f"  {line}" for line in lines))
    totals = {**totals_json(scored.totals), **scored.score.as_json(), "order_items": dict(ordered)}
    return {"cases": found, "totals": totals}


def order_of(visit_id: str) -> tuple[list[Any], Counter[str]]:
    """The visit's order items as said, each with its catalog item's key if it links to one,
    and how many went each of ORDER_WAYS: linked; to review, a name the catalog hasn't got (or
    has twice); or other, a competitor's name, say."""
    with session_scope(read_only=True) as session:
        query = (
            select(OrderItem.name, OrderItem.quantity, OrderItem.unit, CatalogItem.key)
            .outerjoin(CatalogItem, CatalogItem.id == OrderItem.sku_id)
            .where(OrderItem.visit_id == visit_id)
            .order_by(OrderItem.seq)
        )
        order = list(session.execute(query))
        reviewed = set(
            session.scalars(
                select(AliasReview.normalised).where(
                    AliasReview.visit_id == visit_id, AliasReview.kind == CatalogKind.OWN
                )
            )
        )
    ways = Counter(
        "linked"
        if row.key is not None
        else "to_review"
        if entities.link_key(row.name) in reviewed
        else "other"
        for row in order
    )
    return order, ways


def order_lines(order: Sequence[Any], ways: Counter[str]) -> list[str]:
    """The count, then each item: rp05's case is an order and nothing else (batch 14)."""
    lines = [f"order items: {len(order)} ({buckets(ways, ORDER_WAYS)})"]
    for row in order:
        quantity = "no quantity said" if row.quantity is None else f"{row.quantity.normalize():f}"
        amount = f"{quantity} {row.unit}" if row.unit else quantity
        lines.append(f"  {row.name}: {amount} -> {row.key or 'not in the catalog'}")
    return lines


def write_results(results: dict[str, Any], packs: Packs, model: str) -> Path:
    """data/evals/results/{date}-{prompt_hash}.json; a second run that day, -2, -3, …"""
    today, hashed = ist_date(utc_now()).isoformat(), prompt_hash(packs)
    stem = f"{today}-{hashed}"
    folder = EVALS_DIR / "results"
    path = folder / f"{stem}.json"
    number = 1
    while path.exists():
        number += 1
        path = folder / f"{stem}-{number}.json"
    header = {
        "date": today,
        "prompt_hash": hashed,
        "model": model,
        "packs": [{"id": p.id, "version": p.version} for p in packs.values()],
        "playbooks": sorted({f"{p.playbook.id} v{p.playbook.version}" for p in packs.values()}),
    }
    body = json.dumps(header | results, ensure_ascii=False, indent=1) + "\n"
    try:
        storage.write_file(path, body.encode())
    except OSError as error:
        raise Refused(f"results not written: {path} ({type(error).__name__})") from None
    return path


def main(argv: list[str], llm: StructuredLlm | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_eval.py")
    parser.add_argument("names", nargs="*", help="the cases to run (every one if none)")
    parser.add_argument("--yes", action="store_true", help="send them, and pay")
    parser.add_argument("--cases", type=Path, default=CASES, help="the eval cases' folder")
    parser.add_argument("--root", type=Path, default=ORG_PACKS_DIR, help="the org packs' folder")
    parser.add_argument(
        "--playbooks", type=Path, default=PLAYBOOKS_DIR, help="the playbooks' folder"
    )
    args = parser.parse_args(argv)
    folders = {"packs": args.root, "playbooks": args.playbooks}
    try:
        cases = load_cases(args.cases, args.names)
        keys = sorted({(c.file.org_pack.id, c.file.org_pack.version) for c in cases})
        packs = {key: load_org_pack(*key, args.root, args.playbooks) for key in keys}
        check_roles(cases, packs)
        if args.yes:
            llm = llm or model_llm()  # before anything is made, so a run without a key is quiet
            if llm is None:
                return 1
        use_eval_database()
        prepare_database()
        visits = prepare_visits(cases, packs)
        if not args.yes:
            return dry_run(cases, visits, packs)
        assert llm is not None
        with paid_run(dict.fromkeys(visits.values(), 0), heading="Paid:") as paid:
            states = run(cases, visits, llm, folders)
        if isinstance(states, int):
            return states
        path = write_results(report(cases, visits, states, packs, paid), packs, llm.model)
        print(f"\nResults: {path}")
        return 0 if all(state == JobState.DONE for state in states.values()) else 1
    except Refused as why:
        print(f"{why}", file=sys.stderr)
        return 1
    except SETUP_ERRORS as error:
        print(f"failed: {setup_trouble(error)}", file=sys.stderr)
        return 1
    except DomainError as error:  # a pack or answer key that doesn't check out
        print(f"failed: {describe(error)}", file=sys.stderr)
        return 1
    except (Stopped, KeyboardInterrupt):
        print("stopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    configure_logging()  # the job's own lines: each call's tokens, never the prompt
    sys.exit(main(sys.argv[1:]))
