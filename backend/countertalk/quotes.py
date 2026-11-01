"""Checks an extraction answer's quotes against the transcript (batch 11; BRIEF D9).

Each quote is verified (verify.py). One that matches nothing gets at most one repair call
(§9.5): the insight's summary and the cited turn with 2 either side, asking for one exact quote
and its turn. The answer goes through the same search. A repair that fails, times out or comes
back invalid leaves its quote unmatched: it's logged in llm_calls, and it never fails the visit,
since a retry would pay for extraction again. After one fails to reach Meta, the rest aren't
sent; a quote whose cited turn isn't in the transcript has nothing to show, so it isn't either.
A lost claim stops the job (§2.4).

Called in the extract job after extraction, before the write: repairs are paid first, then the
rows are written once. Only counts are logged, never a quote (§1.9).
"""

import logging
import threading
from collections import Counter
from dataclasses import dataclass
from typing import Any

from countertalk import llm_calls
from countertalk.errors import RetryableError
from countertalk.llm.base import Message, Outcome, StructuredLlm
from countertalk.models import LlmPurpose
from countertalk.verify import Transcript, Verified, verify_quote

log = logging.getLogger(__name__)

NAME = "quote_repair"
# Either side of the cited turn, in a repair's request.
NEARBY = 2
INSTRUCTIONS = """\
You find the quote behind a point someone raised in a recorded conversation.

Below are the point, in English, and some turns of the transcript, each as \
[T<turn>][<speaker's role>] what was said. Copy one passage that says the point exactly as it's \
written in one turn: the same words, in the same script and spelling, 40 words or fewer. Never \
translate, correct or join two turns. Give that turn's number."""
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "turn_id": {"type": "integer", "description": "The number after T in the turn's line."},
        "quote": {"type": "string", "description": "Copied exactly from that turn."},
    },
    "required": ["turn_id", "quote"],
    "additionalProperties": False,
}

# (insight's index, quote's index) in the answer.
Key = tuple[int, int]


@dataclass(frozen=True)
class Checked:
    results: dict[Key, Verified]


def check_quotes(
    visit_id: str,
    answer: dict[str, Any],
    transcript: Transcript,
    llm: StructuredLlm,
    *,
    claim_lost: threading.Event | None = None,
) -> Checked:
    """Raises the client's claim_lost if the job's claim is lost: another worker may have it."""
    results: dict[Key, Verified] = {}
    counts = {"repairs": 0, "unrepairable": 0}
    reachable = True  # after a repair fails to reach Meta, the rest aren't sent
    for i, insight in enumerate(answer["insights"]):
        for j, quoted in enumerate(insight["evidence"]):
            found = verify_quote(quoted["quote"], quoted["turn_id"], transcript)
            if found.match == "none" and found.turn_id is None:
                counts["unrepairable"] += 1  # no cited turn to show
            elif found.match == "none" and reachable:
                outcome = _repair(insight["summary_en"], found, transcript, llm, claim_lost)
                llm_calls.record(visit_id, LlmPurpose.QUOTE_REPAIR, llm.provider, outcome)
                counts["repairs"] += bool(outcome.calls)
                if outcome.error is not None:
                    if outcome.error.code == "claim_lost":
                        raise outcome.error
                    reachable = not isinstance(outcome.error, RetryableError)
                found = _checked(outcome.answer, found, transcript)
            results[(i, j)] = found
    matches = Counter(found.match for found in results.values())
    log.info(
        "quotes checked visit=%s exact=%d fuzzy=%d none=%d repairs=%d unrepairable=%d",
        visit_id,
        matches["exact"],
        matches["fuzzy"],
        matches["none"],
        counts["repairs"],
        counts["unrepairable"],
    )
    return Checked(results)


def _repair(
    summary: str,
    found: Verified,
    transcript: Transcript,
    llm: StructuredLlm,
    claim_lost: threading.Event | None,
) -> Outcome:
    """The one repair request for an unmatched quote: the point, and its cited turn with
    NEARBY either side."""
    at = transcript.index[found.turn_id]  # type: ignore[index]  # check_quotes: it's there
    nearby = transcript.turns[max(0, at - NEARBY) : at + NEARBY + 1]
    lines = [f"[T{t.turn_id}][{t.role or 'other'}] {' '.join(t.text.split())}" for t in nearby]
    messages = [
        Message("system", INSTRUCTIONS),
        Message("user", f"The point: {summary}\n\nThe turns:\n" + "\n".join(lines)),
    ]
    return llm.complete(messages, NAME, SCHEMA, claim_lost=claim_lost)


def _checked(answer: dict[str, Any] | None, found: Verified, transcript: Transcript) -> Verified:
    """A repair's answer through the same search; unmatched, the quote stays as it was."""
    if answer is None:
        return found
    quote, turn_id = answer.get("quote"), answer.get("turn_id")
    if not isinstance(quote, str) or isinstance(turn_id, bool) or not isinstance(turn_id, int):
        return found  # the client checked the schema: this is R4's belt and braces
    repaired = verify_quote(quote, turn_id, transcript)
    return repaired if repaired.match != "none" else found
