"""Speaker roles (batch 9; BRIEF §12 "Speaker roles").

Muse Spark decides who each of a transcript's speaker labels is: one of its playbook's roles.
Labels are per chunk (c0:A, c1:A), and two labels, in one chunk or in two, can be the same person:
each label simply gets its own role.

- What's sent: the first ROLES_FIRST_TURNS turns, ROLES_SAMPLED_TURNS spread evenly over the
  rest, and the longest turn of any label not in those yet.
- Every label gets a role. One the reply leaves out is `other`, with confidence 0; an entry that
  doesn't fit (another label, a role the playbook hasn't got, a confidence outside 0 to 1) is
  ignored, and a label placed twice keeps its first. A reply that places nobody is asked once
  more, then fails.
- Saved once, with the playbook's ID and version, and never asked for again (§9.4). The check is
  made again in the write, under the visit's lock: a run that finishes second keeps the first's.
- No transaction is open during a call (§2.4), and every call is logged in llm_calls, failed ones
  too (§9.3). The prompt and the answer are never logged (§1.9).
"""

import json
import logging
import threading
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from countertalk import limits, llm_calls, visits
from countertalk.db import session_scope
from countertalk.errors import Conflict, NotFound, PermanentError
from countertalk.llm.base import CallDetails, Message, StructuredLlm
from countertalk.models import LlmPurpose, Org, SpeakerRole, Transcript, Turn, Visit, parse_id
from countertalk.playbooks import OrgPack, playbook_text

log = logging.getLogger(__name__)

NAME = "speaker_roles"
UNPLACED = ("other", 0.0)
INSTRUCTIONS = """\
You decide who each speaker in a recorded conversation is.

Each line is one turn: [T<turn>][<speaker label>][<minutes:seconds from the start>] what was said.
Speech-to-text names the speakers within each chunk of the recording, so a label starts with
its chunk: c0:A is speaker A in chunk 0, and c1:A is speaker A in chunk 1. A label ending in ?
holds words it couldn't give to a speaker.
- Labels in different chunks can be the same person: c0:A and c1:B may both be the rep.
- Two labels in one chunk can also be the same person: speech-to-text sometimes splits one voice
  in two, such as c0:A and c0:B.
- A long transcript is shortened: you see its first turns, a sample of the rest, and at least one
  turn of every label.

Give every label on the Labels line one role, with your confidence from 0 to 1.

The roles:"""
# A reply that placed nobody is asked once more, told so.
REASK = "That answer placed none of the labels on the Labels line. Give each of them one role."


@dataclass(frozen=True)
class Assigned:
    # label: (role, confidence), for every label in the transcript.
    roles: dict[str, tuple[str, float]]
    # This run's calls; none when the roles were saved already.
    calls: tuple[CallDetails, ...]


@dataclass(frozen=True)
class _Turn:
    turn_id: int
    label: str
    start_ms: int
    text: str


def assign_roles(
    transcript_id: str,
    llm: StructuredLlm,
    pack: OrgPack,
    *,
    claim_lost: threading.Event | None = None,
) -> Assigned:
    """The transcript's roles: the saved ones, or asked for and saved. `pack` is its visit's
    org's own (Conflict otherwise); a job's handler passes its `claim_lost` (§2.4)."""
    found = parse_id(transcript_id)
    with session_scope(read_only=True) as session:
        transcript = session.get(Transcript, found) if found is not None else None
        if transcript is None:
            raise NotFound("transcript_not_found", "There's no such transcript")
        transcript_id = transcript.id  # as the database writes it
        visit = session.get_one(Visit, transcript.visit_id)
        org = session.get_one(Org, visit.org_id)
        if (org.org_pack_id, org.org_pack_version) != (pack.id, pack.version):
            raise Conflict("wrong_org_pack", "That isn't the org pack of the visit's org")
        visit_id, org_id = visit.id, visit.org_id
        saved = _saved(session, transcript_id)
        if saved:
            return Assigned(saved, ())
        query = select(Turn.turn_id, Turn.speaker_label, Turn.start_ms, Turn.text)
        turns = [
            _Turn(*row)
            for row in session.execute(
                query.where(Turn.transcript_id == transcript_id).order_by(Turn.turn_id)
            )
        ]
    if not turns:
        raise PermanentError("transcript_empty", "The transcript has no turns")
    labels = list(dict.fromkeys(turn.label for turn in turns))
    role_keys = list(pack.playbook.roles)
    sent = sample(turns, labels)
    messages = [
        Message("system", INSTRUCTIONS + "\n\n" + playbook_text(pack, "roles_prompt")),
        Message("user", f"Labels: {', '.join(labels)}\n\n" + "\n".join(map(_line, sent))),
    ]
    log.info(
        "roles asked visit=%s transcript=%s labels=%d turns_sent=%d of=%d",
        visit_id,
        transcript_id,
        len(labels),
        len(sent),
        len(turns),
    )
    calls: list[CallDetails] = []
    for attempt in (1, 2):  # a reply that places nobody is asked once more
        outcome = llm.complete(messages, NAME, _schema(labels, role_keys), claim_lost=claim_lost)
        calls.extend(outcome.calls)
        llm_calls.record(visit_id, LlmPurpose.ROLES, llm.provider, outcome)
        if outcome.error is not None:
            raise outcome.error
        answer = outcome.answer or {}
        placed, ignored = _placed(answer, labels, role_keys)
        if placed:
            break
        log.warning(
            "roles reply placed nobody visit=%s attempt=%d ignored=%d", visit_id, attempt, ignored
        )
        reply = json.dumps(answer, ensure_ascii=False)
        messages = [*messages, Message("assistant", reply), Message("user", REASK)]
    else:
        raise PermanentError("roles_none_placed", "Muse Spark didn't say who any speaker was")
    roles = {label: placed.get(label, UNPLACED) for label in labels}
    with session_scope() as session:
        visits.lock_visit(session, org_id, visit_id)
        saved = _saved(session, transcript_id)
        if saved:
            log.info("roles kept from another run visit=%s transcript=%s", visit_id, transcript_id)
            return Assigned(saved, tuple(calls))
        session.add_all(
            SpeakerRole(
                transcript_id=transcript_id,
                speaker_label=label,
                role=role,
                confidence=confidence,
                playbook_id=pack.playbook.id,
                playbook_version=pack.playbook.version,
            )
            for label, (role, confidence) in roles.items()
        )
    log.info(
        "roles assigned visit=%s transcript=%s roles=%s unplaced=%d ignored=%d",
        visit_id,
        transcript_id,
        ",".join(f"{label}={role}" for label, (role, _) in roles.items()),
        len(labels) - len(placed),
        ignored,
    )
    return Assigned(roles, tuple(calls))


def sample(turns: list[_Turn], labels: list[str]) -> list[_Turn]:
    """The turns to send, in order (BRIEF §12)."""
    first, rest = turns[: limits.ROLES_FIRST_TURNS], turns[limits.ROLES_FIRST_TURNS :]
    count = limits.ROLES_SAMPLED_TURNS
    spread = rest if len(rest) <= count else [rest[i * len(rest) // count] for i in range(count)]
    chosen = {turn.turn_id: turn for turn in first + spread}
    seen = {turn.label for turn in chosen.values()}
    for label in labels:
        if label not in seen:
            # Its longest turn says the most about who it is; the earliest of equals.
            longest = max((t for t in turns if t.label == label), key=lambda t: len(t.text))
            chosen[longest.turn_id] = longest
    return sorted(chosen.values(), key=lambda turn: turn.turn_id)


def _line(turn: _Turn) -> str:
    seconds = turn.start_ms // 1000
    words = " ".join(turn.text.split())  # one line per turn
    return f"[T{turn.turn_id}][{turn.label}][{seconds // 60:02d}:{seconds % 60:02d}] {words}"


def _schema(labels: list[str], role_keys: list[str]) -> dict[str, Any]:
    speaker = {
        "type": "object",
        "properties": {
            "label": {"type": "string", "enum": labels},
            "role": {"type": "string", "enum": role_keys},
            "confidence": {"type": "number", "description": "From 0 to 1"},
        },
        "required": ["label", "role", "confidence"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"speakers": {"type": "array", "items": speaker}},
        "required": ["speakers"],
        "additionalProperties": False,
    }


def _placed(
    answer: dict[str, Any], labels: list[str], role_keys: list[str]
) -> tuple[dict[str, tuple[str, float]], int]:
    """Each label the answer places, with its role and confidence, and how many entries didn't
    fit and were ignored, for the log. Strict, though the client checked the schema (R4)."""
    placed: dict[str, tuple[str, float]] = {}
    ignored = 0
    entries = answer.get("speakers")
    for entry in entries if isinstance(entries, list) else []:
        fields = entry if isinstance(entry, dict) else {}
        label, role, confidence = fields.get("label"), fields.get("role"), fields.get("confidence")
        fits = label in labels and role in role_keys and label not in placed
        if isinstance(confidence, bool) or not isinstance(confidence, int | float):
            fits = False
        elif not 0 <= confidence <= 1:  # NaN isn't either
            fits = False
        if fits:
            placed[label] = (role, float(confidence))
        else:
            ignored += 1
    return placed, ignored


def _saved(session: Session, transcript_id: str) -> dict[str, tuple[str, float]]:
    """The saved roles, in the order their labels are first heard, as a first run gives them."""
    first_heard = (
        select(Turn.speaker_label, func.min(Turn.turn_id).label("turn_id"))
        .where(Turn.transcript_id == transcript_id)
        .group_by(Turn.speaker_label)
        .subquery()
    )
    rows = session.execute(
        select(SpeakerRole.speaker_label, SpeakerRole.role, SpeakerRole.confidence)
        .join(first_heard, first_heard.c.speaker_label == SpeakerRole.speaker_label)
        .where(SpeakerRole.transcript_id == transcript_id)
        .order_by(first_heard.c.turn_id)
    )
    return {label: (role, confidence) for label, role, confidence in rows}
