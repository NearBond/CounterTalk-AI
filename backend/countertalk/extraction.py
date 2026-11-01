"""Extraction (batch 10; BRIEF §12): Muse Spark reads a visit's transcript, with its speakers'
roles, against the playbook and the org's pack, and returns the visit's insights, tasks and
report.

The request, in an order that lets its start be cached across visits:
1. the playbook's prompt.md and its codebook (each category's definition, includes, excludes);
2. the org pack's part: what the org sells, its brands and its competitors' (the org's own
   catalog), its teams with what each handles, and the schemes running near the visit's date;
3. the transcript, each turn with its speaker's role.

The `extract` job (handler in worker.py): lock the visit and set `extracting`; check it fits
Muse Spark's context; roles, reusing saved ones (§9.4); extraction; every quote checked and the
unmatched repaired once (quotes.py, batch 11); then, in one transaction
under the visit's lock, an Extraction row, the derived rows replaced, each insight linked to the
org's catalog (entities.py) and each verified one routed (routing.py, batch 12), and `review`.
Only while the visit is still `extracting`: a run that finds it moved on (failed, say) writes
nothing.
"""

import logging
import threading
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from countertalk import entities, jobs, limits, llm_calls, quotes, roles, routing, visits
from countertalk.db import session_scope
from countertalk.errors import Conflict, NotFound, PermanentError, RetryableError
from countertalk.llm import pricing
from countertalk.llm.base import Message, StructuredLlm
from countertalk.models import (
    ACTIVE_JOB,
    CatalogItem,
    CatalogKind,
    Evidence,
    Extraction,
    Insight,
    Job,
    JobKind,
    LlmPurpose,
    Org,
    Report,
    Task,
    Team,
    Visit,
    VisitKind,
    VisitStatus,
    ist_date,
    new_id,
)
from countertalk.playbooks import (
    ISO_DATE,
    KEY,
    ORG_PACKS_DIR,
    PLAYBOOKS_DIR,
    OrgPack,
    Scheme,
    build_extraction_schema,
    load_org_pack,
    playbook_text,
)
from countertalk.verify import Transcript, TurnText

log = logging.getLogger(__name__)

NAME = "extraction"


@dataclass(frozen=True)
class Request:
    visit_id: str
    org_id: str
    transcript_id: str
    visit_date: date
    turns: int
    messages: list[Message]
    schema: dict[str, Any]
    # The turns it was built from, for checking the answer's quotes.
    turn_rows: tuple[roles.SentTurn, ...]


def pack_for(org_id: str, packs: Path = ORG_PACKS_DIR, playbooks: Path = PLAYBOOKS_DIR) -> OrgPack:
    """The org's own pack (BRIEF §12: each org uses only its own)."""
    with session_scope(read_only=True) as session:
        org = session.get_one(Org, org_id)
        pack_id, version = org.org_pack_id, org.org_pack_version
    if pack_id is None or version is None:
        raise PermanentError(
            "no_org_pack", "The visit's organisation isn't set up with an org pack"
        )
    return load_org_pack(pack_id, version, packs, playbooks)


def request_for(visit_id: str, pack: OrgPack, speaker_roles: dict[str, str]) -> Request:
    """Reads what it needs in one short transaction, and builds the request with none open."""
    with session_scope(read_only=True) as session:
        visit = session.get(Visit, visit_id)
        if visit is None or visit.transcript_id is None:
            raise NotFound("transcript_not_found", "The visit has no transcript")
        org_id, transcript_id = visit.org_id, visit.transcript_id
        visit_date = ist_date(visit.created_at)
        turns = roles.load_turns(session, transcript_id)
        items = session.execute(
            select(CatalogItem.key, CatalogItem.kind, CatalogItem.company)
            .add_columns(CatalogItem.brand, CatalogItem.product)
            .where(CatalogItem.org_id == org_id, CatalogItem.active.is_(True))
            .order_by(CatalogItem.kind != CatalogKind.OWN, CatalogItem.key)
        ).all()
    # A label with no role yet (a dry run, or the size check before roles are paid for) gets
    # the playbook's longest role, so the request is never shorter than the real one.
    longest = max(pack.playbook.roles, key=len)
    lines = [
        roles.turn_line(t.turn_id, speaker_roles.get(t.label, longest), t.start_ms, t.text)
        for t in turns
    ]
    messages = [
        Message("system", playbook_part(pack)),
        Message("system", org_part(pack, items, visit_date)),
        Message("user", f"The visit, on {visit_date.isoformat()}:\n\n" + "\n".join(lines)),
    ]
    schema = build_extraction_schema(pack.playbook, pack)
    return Request(
        visit_id, org_id, transcript_id, visit_date, len(turns), messages, schema, tuple(turns)
    )


def playbook_part(pack: OrgPack) -> str:
    """prompt.md, then the playbook's codebook: the same for every visit of every org on it."""
    book = pack.playbook

    def described(title: str, entries: dict[str, Any]) -> list[str]:
        return [f"\n## {title}", *(f"- `{k}`: {v.description}" for k, v in entries.items())]

    parts = [playbook_text(pack, "prompt"), "\n# The codebook"]
    parts += described("Kinds", book.kinds)
    parts += [
        "\n## Severities, highest first",
        *(f"- `{k}`: {v.description}" for k, v in book.severities.items()),
    ]
    parts += described("Competitor reasons", book.competitor_reasons)
    parts += described("Task types", book.task_types)
    parts += described("Task owners", book.task_owners)
    parts.append("\n## Categories")
    for key, category in book.categories.items():
        parts.append(f"\n### `{key}`: {category.name}\n{category.definition}")
        parts += ["Includes, for example:", *(f'- "{text}"' for text in category.includes)]
        if category.excludes:
            parts.append("Not this category:")
            parts += [f"- {e.text} That's `{e.category}`." for e in category.excludes]
    return "\n".join(parts)


def org_part(pack: OrgPack, items: Sequence[Any], visit_date: date) -> str:
    """The org pack's part: the same for every visit of the org, but for its last lines, the
    schemes running near the visit's date."""
    own: dict[str, list[str]] = defaultdict(list)
    theirs: dict[tuple[str, str], list[str]] = defaultdict(list)
    names = {}
    for key, kind, company, brand, product in items:
        names[key] = f"{brand} {product}" if product else brand
        found = own[brand] if kind == CatalogKind.OWN else theirs[(company, brand)]
        if product:
            found.append(product)
    parts = [f"# The company: {pack.file.display_name}", pack.file.sells.strip()]
    parts.append("\n## Its brands and products")
    parts += [f"- {b}" + (f": {', '.join(p)}" if p else "") for b, p in own.items()]
    parts.append("\n## Competitors' brands and products")
    parts += [f"- {b} ({c})" + (f": {', '.join(p)}" if p else "") for (c, b), p in theirs.items()]
    parts.append("\n## Its teams, and what each handles")
    parts += [f"- `{k}` ({t.name}): {t.handles.strip()}" for k, t in pack.file.teams.items()]
    nearby = schemes_near(pack.schemes, visit_date)
    parts.append(f"\n## Its schemes running within {limits.SCHEME_WINDOW_DAYS} days of the visit")
    parts += [
        f"- `{s.key}`: {s.name} ({s.kind}, for {', '.join(names.get(c, c) for c in s.covers)};"
        f" {s.starts.isoformat()} to {s.ends.isoformat()})"
        for s in nearby
    ] or ["None."]
    return "\n".join(parts)


def schemes_near(schemes: Sequence[Scheme], visit_date: date) -> list[Scheme]:
    """Those running at some point within SCHEME_WINDOW_DAYS of the visit's date."""
    window = timedelta(days=limits.SCHEME_WINDOW_DAYS)
    return [s for s in schemes if s.starts <= visit_date + window and s.ends >= visit_date - window]


def too_long(request: Request) -> bool:
    """Whether the request, with room for the answer, may not fit Muse Spark's context."""
    needed = pricing.input_tokens(request.messages, request.schema)
    return needed + limits.LLM_OUTPUT_RESERVE_TOKENS > limits.LLM_CONTEXT_TOKENS


def extract(
    visit_id: str,
    llm: StructuredLlm,
    *,
    claim_lost: threading.Event | None = None,
    packs: Path = ORG_PACKS_DIR,
    playbooks: Path = PLAYBOOKS_DIR,
) -> None:
    """The extract job's work (module docstring). Raises PermanentError or RetryableError."""
    started = _start(visit_id)
    if started is None:
        return
    org_id, transcript_id = started
    pack = pack_for(org_id, packs, playbooks)
    # Checked before paying for roles, with the longest role in every turn.
    if too_long(request_for(visit_id, pack, {})):
        raise PermanentError(
            "transcript_too_long", "The visit is too long for Muse Spark to read in one go"
        )
    assigned = roles.assign_roles(transcript_id, llm, pack, claim_lost=claim_lost)
    request = request_for(visit_id, pack, {k: role for k, (role, _) in assigned.roles.items()})
    if request.transcript_id != transcript_id:
        raise RetryableError("transcript_changed", "The visit's transcript changed meanwhile")
    log.info("extraction asked visit=%s turns=%d", visit_id, request.turns)
    outcome = llm.complete(request.messages, NAME, request.schema, claim_lost=claim_lost)
    llm_calls.record(visit_id, LlmPurpose.EXTRACTION, llm.provider, outcome)
    if outcome.error is not None:
        if outcome.error.code == "llm_answer_invalid":
            raise PermanentError(
                "extraction_invalid",
                "Extraction invalid: Muse Spark's answer didn't fit, even after a repair",
            ) from None
        raise outcome.error
    assert outcome.answer is not None
    answer = _no_nul(outcome.answer)
    # Every quote checked, and the unmatched repaired, before the write (batch 11).
    transcript = _transcript(request, assigned.roles)
    checked = quotes.check_quotes(visit_id, answer, transcript, llm, claim_lost=claim_lost)
    linked = _link(request, pack, answer)
    # The request's hash: a repair's includes the answer it repaired.
    prompt_hash = outcome.calls[0].prompt_hash
    _save(request, pack, llm.model, prompt_hash, answer, checked, linked)


@dataclass(frozen=True)
class Linked:
    """Each insight's links and kept scheme, in the answer's order, and what linking found."""

    links: tuple[entities.Links, ...]
    schemes: tuple[str | None, ...]
    tally: Counter[str]


def _link(request: Request, pack: OrgPack, answer: dict[str, Any]) -> Linked:
    """Linking, before the save takes the visit's lock: matching names never holds it (§2.3).
    A scheme key dropped is logged, as a key, never other text (§1.9)."""
    with session_scope(read_only=True) as session:
        catalog = entities.load_catalog(session, request.org_id)
    schemes = entities.scheme_names(pack.schemes)
    tally: Counter[str] = Counter()
    links, kept = [], []
    for number, found in enumerate(answer["insights"]):
        mentioned = (found["sku_mentions"], found["competitor_mentions"])
        links.append(entities.link_mentions(catalog, *mentioned, tally))
        given = found["scheme_key"]
        kept.append(entities.link_scheme(schemes, given, tally))
        if given is not None and kept[-1] is None:
            shown = given if KEY.fullmatch(given) else "(not a key)"
            log.info("scheme dropped visit=%s insight=%d key=%s", request.visit_id, number, shown)
    return Linked(tuple(links), tuple(kept), tally)


def _transcript(request: Request, speaker_roles: dict[str, tuple[str, float]]) -> Transcript:
    return Transcript(
        [
            TurnText(
                t.turn_id, speaker_roles.get(t.label, (None,))[0], t.start_ms, t.end_ms, t.text
            )
            for t in request.turn_rows
        ]
    )


def _start(visit_id: str) -> tuple[str, str] | None:
    """Locks the visit and sets `extracting`: its org and transcript, or None if there's
    nothing to do."""
    with session_scope() as session:
        visit = visits.lock_for_work(session, visit_id)
        if visit is None or visit.status not in (VisitStatus.TRANSCRIBED, VisitStatus.EXTRACTING):
            status = None if visit is None else visit.status
            log.info("extract skipped visit=%s status=%s", visit_id, status)
            return None
        if visit.kind == VisitKind.NOTES:
            raise PermanentError("notes_not_extracted", "Notes visits aren't extracted yet")
        if visit.transcript_id is None:
            raise PermanentError("no_transcript", "The visit has no transcript to extract")
        if visit.status == VisitStatus.TRANSCRIBED:
            visits.set_status(visit, VisitStatus.EXTRACTING)
        return visit.org_id, visit.transcript_id


def _save(
    request: Request,
    pack: OrgPack,
    model: str,
    prompt_hash: str,
    answer: dict[str, Any],
    checked: quotes.Checked,
    linked: Linked,
) -> None:
    """One transaction under the visit's lock: only while it's still `extracting` (§2.3). Each
    quote is written with what checking it found; an insight with a matched quote is verified,
    and only a verified one is routed (D9). Its links and routes are written with it."""
    visit_id = request.visit_id
    with session_scope() as session:
        visit = visits.lock_for_work(session, visit_id)
        if visit is None or visit.status != VisitStatus.EXTRACTING:
            status = None if visit is None else visit.status
            log.info("extraction not saved visit=%s status=%s", visit_id, status)
            return
        query = select(Team.key, Team.id).where(Team.org_id == visit.org_id)
        teams = {key: team_id for key, team_id in session.execute(query)}
        routes = routing.table_of(pack)
        extraction = Extraction(
            id=new_id(),
            visit_id=visit_id,
            transcript_id=request.transcript_id,
            playbook_id=pack.playbook.id,
            playbook_version=pack.playbook.version,
            org_pack_id=pack.id,
            org_pack_version=pack.version,
            model=model,
            prompt_hash=prompt_hash,
            raw_json=answer,
        )
        session.add(extraction)
        session.flush()  # first: the report and insights refer to it
        for table in (Report, Task):
            session.execute(delete(table).where(table.visit_id == visit_id))
        # Only extracted insights are replaced: one the rep added stays (batch 14). An insight's
        # evidence goes with it.
        extracted = Insight.extraction_id.is_not(None)
        session.execute(delete(Insight).where(Insight.visit_id == visit_id, extracted))
        kept = select(func.max(Insight.seq)).where(Insight.visit_id == visit_id)
        first_seq = (session.scalar(kept) or -1) + 1
        report_fields = {k: v for k, v in answer.items() if k not in ("insights", "tasks")}
        session.add(
            Report(
                visit_id=visit_id,
                extraction_id=extraction.id,
                summary_en=answer["summary_en"],
                next_visit_date=_date(answer["next_visit_date"]),
                json=report_fields,
            )
        )
        session.add_all(
            Task(
                visit_id=visit_id,
                seq=seq,
                type=task["type"],
                description_en=task["description_en"],
                due=_date(task["due"]),
                owner=task["owner"],
            )
            for seq, task in enumerate(answer["tasks"])
        )
        unknown = {"turns": 0, "teams": 0}
        evidence: list[Evidence] = []
        to_route: list[tuple[Insight, routing.Decision]] = []
        for number, found in enumerate(answer["insights"]):
            seq = first_seq + number
            results = [checked.results[(number, j)] for j in range(len(found["evidence"]))]
            team = found["suggested_team"]
            unknown["teams"] += team is not None and team not in teams
            links = linked.links[number]
            insight = Insight(
                id=new_id(),
                org_id=visit.org_id,
                visit_id=visit_id,
                seq=seq,
                extraction_id=extraction.id,
                kind=found["kind"],
                category=found["category"],
                severity=found["severity"],
                summary_en=found["summary_en"],
                sku_mentions=found["sku_mentions"],
                competitor_mentions=found["competitor_mentions"],
                competitor_reason=found["competitor_reason"],
                sku_id=links.sku_id,
                competitor_id=links.competitor_id,
                scheme_key=linked.schemes[number],
                ai_team_id=teams.get(team) if team is not None else None,
                ai_team_reason=found["team_reason"],
                # Unverified, it never reaches a team (BRIEF D9).
                verified=any(result.match != "none" for result in results),
            )
            session.add(insight)
            decision = routing.decide(found["category"], found["severity"], team, routes, teams)
            to_route.append((insight, decision))
            for position, (quoted, result) in enumerate(
                zip(found["evidence"], results, strict=True)
            ):
                # Where it was found; unmatched, the cited turn, or none if the transcript hasn't
                # got it. Kept either way, never dropped (§9.6).
                unknown["turns"] += result.turn_id is None
                evidence.append(
                    Evidence(
                        insight_id=insight.id,
                        visit_id=visit_id,
                        seq=position,
                        transcript_id=request.transcript_id,
                        turn_id=result.turn_id,
                        speaker_role=result.speaker_role,
                        quote_llm=quoted["quote"],
                        quote_final=result.quote_final,
                        match=result.match,
                        char_start=result.char_start,
                        char_end=result.char_end,
                        start_ms=result.start_ms,
                    )
                )
        # Two flushes in all: the evidence refers to its insights, and the unit of work only
        # orders inserts along relationships, which these tables don't declare.
        session.flush()
        session.add_all(evidence)
        reviews = [review for links in linked.links for review in links.reviews]
        new_reviews = entities.add_reviews(session, visit.org_id, visit_id, reviews)
        routed = Counter(
            routing.route_new(session, insight, decision, teams) for insight, decision in to_route
        )
        visits.set_status(visit, VisitStatus.REVIEW)
    log.info(
        "extracted visit=%s insights=%d tasks=%d unknown_turns=%d unknown_teams=%d",
        visit_id,
        len(answer["insights"]),
        len(answer["tasks"]),
        unknown["turns"],
        unknown["teams"],
    )
    log.info(
        "linked visit=%s exact=%d fuzzy=%d unknown=%d ambiguous=%d wrong_kind=%d new_reviews=%d"
        " scheme_by_alias=%d scheme_dropped=%d",
        visit_id,
        *(linked.tally[k] for k in ("exact", "fuzzy", "unknown", "ambiguous", "wrong_kind")),
        new_reviews,
        linked.tally["scheme_by_alias"],
        linked.tally["scheme_dropped"],
    )
    log.info(
        "routed visit=%s routed=%d triage=%d unverified=%d",
        visit_id,
        routed["routed"],
        routed["triage"],
        routed["unverified"],
    )


def _date(value: object) -> date | None:
    """A YYYY-MM-DD the answer gave, or None: an impossible date is left out, not guessed."""
    if not isinstance(value, str) or ISO_DATE.fullmatch(value) is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _no_nul(value: Any) -> Any:
    """The answer with NUL characters taken out of its text: Postgres refuses them, so one would
    fail the write on every retry, and each retry pays again."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_no_nul(item) for item in value]
    if isinstance(value, dict):
        return {key: _no_nul(item) for key, item in value.items()}
    return value


def wait_for_key(visit_id: str) -> bool:
    """Without MODEL_API_KEY: True if the visit waits at `transcribed` for one, its job done."""
    with session_scope(read_only=True) as session:
        visit = session.get(Visit, visit_id)
        status = None if visit is None else visit.status
        notes = visit is not None and visit.kind == VisitKind.NOTES
    if status == VisitStatus.EXTRACTING:
        # It can't step back to wait: failed with a reason, and extracted again once a key is set.
        raise PermanentError(
            "llm_not_configured", "Extraction stopped: the server has no MODEL_API_KEY"
        )
    if status != VisitStatus.TRANSCRIBED or notes:
        return False  # nothing to wait for: the job runs, and does nothing or fails clearly
    log.info("extraction is off, visit waits visit=%s", visit_id)
    return True


def queue_waiting(limit: int) -> int:
    """Queues extraction for up to `limit` visits waiting at `transcribed` with no extract job,
    oldest first: housekeeping, once a key is set. Each in its own transaction, under the
    visit's lock (R6). How many it queued."""
    # Jobs with a visit only: one NULL would make NOT IN match nothing.
    active = select(Job.visit_id).where(
        Job.kind == JobKind.EXTRACT, Job.visit_id.is_not(None), text(ACTIVE_JOB)
    )
    with session_scope(read_only=True) as session:
        waiting = session.scalars(
            select(Visit.id)
            .where(Visit.status == VisitStatus.TRANSCRIBED, Visit.id.not_in(active))
            .order_by(Visit.updated_at, Visit.seq)
            .limit(limit)
        ).all()
    queued = 0
    for visit_id in waiting:
        with session_scope() as session:
            visit = visits.lock_for_work(session, visit_id)
            if visit is not None and visit.status == VisitStatus.TRANSCRIBED:
                queued += jobs.enqueue(session, JobKind.EXTRACT, visit_id=visit_id) is not None
    return queued


def queue_again(session: Session, org_id: str, visit_id: str) -> str | None:
    """Re-extraction (batch 10's manual step; batch 13's reprocess): from review or failed, back
    to `transcribed` with an extract job queued, under the visit's lock. Never after confirmed.
    The transcript and any saved roles are reused: only what's missing is paid for."""
    visit = visits.lock_visit(session, org_id, visit_id)
    if visit.status == VisitStatus.CONFIRMED:
        raise Conflict("visit_confirmed", "A confirmed visit can't be extracted again")
    if visit.status not in (VisitStatus.REVIEW, VisitStatus.FAILED):
        raise Conflict("visit_processing", "The visit is still being processed")
    if visit.transcript_id is None:
        raise Conflict("no_transcript", "The visit has no transcript to extract")
    visits.set_status(visit, VisitStatus.TRANSCRIBED)
    return jobs.enqueue(session, JobKind.EXTRACT, visit_id=visit_id)
