"""The llm_calls log (batch 9; BRIEF §8, §12): one row per request to the LLM, failed ones too.

The caller writes it from the Outcome's call details, after every call and before acting on the
answer or raising the error (§9.3). It never holds the prompt or the answer (§1.9).
"""

import logging

from sqlalchemy import func, select

from countertalk.db import session_scope
from countertalk.llm.base import CallDetails, Outcome
from countertalk.models import LlmCall, LlmPurpose

log = logging.getLogger(__name__)


def record(visit_id: str, purpose: LlmPurpose, provider: str, outcome: Outcome) -> None:
    """Logs each call, and a failure's details (§2.10), then writes the calls' rows in one short
    transaction of their own. A failed write is logged, never raised: it mustn't lose an answer
    that was paid for (BRIEF §12)."""
    calls = outcome.calls
    if outcome.error is not None:
        found = outcome.error.details or {}
        log.warning(
            "llm call failed visit=%s purpose=%s code=%s http_status=%s error_type=%s"
            " request_id=%s",
            visit_id,
            purpose,
            outcome.error.code,
            found.get("http_status"),
            found.get("error_type"),
            found.get("request_id"),
        )
    for call in calls:
        log.info(
            "llm call visit=%s purpose=%s model=%s ok=%s error=%s input_tokens=%s"
            " cached_input_tokens=%s output_tokens=%s reasoning_tokens=%s latency_ms=%d"
            " request_id=%s",
            visit_id,
            purpose,
            call.model,
            call.ok,
            call.error,
            call.input_tokens,
            call.cached_input_tokens,
            call.output_tokens,
            call.reasoning_tokens,
            call.latency_ms,
            call.request_id,
        )
    if not calls:
        return
    try:
        with session_scope() as session:
            session.add_all(
                LlmCall(
                    visit_id=visit_id,
                    purpose=purpose,
                    provider=provider,
                    model=call.model,
                    prompt_hash=call.prompt_hash,
                    input_tokens=call.input_tokens,
                    cached_input_tokens=call.cached_input_tokens,
                    output_tokens=call.output_tokens,
                    reasoning_tokens=call.reasoning_tokens,
                    latency_ms=call.latency_ms,
                    request_id=call.request_id,
                    ok=call.ok,
                    error=call.error,
                )
                for call in calls
            )
    except Exception:  # whatever went wrong, the answer goes on (the lines above have the calls)
        log.warning(
            "llm calls not logged visit=%s purpose=%s calls=%d",
            visit_id,
            purpose,
            len(calls),
            exc_info=True,
        )


def latest_seq(visit_id: str) -> int:
    """The visit's newest call's seq, or 0: where a run's own calls start."""
    with session_scope(read_only=True) as session:
        latest = select(func.max(LlmCall.seq)).where(LlmCall.visit_id == visit_id)
        return session.scalar(latest) or 0


def since(
    visit_id: str, seq: int, purpose: LlmPurpose | None = None
) -> list[tuple[str, CallDetails]]:
    """The visit's calls logged after `seq`, with their purpose: what a run paid for, even one
    that failed."""
    query = select(LlmCall).where(LlmCall.visit_id == visit_id, LlmCall.seq > seq)
    if purpose is not None:
        query = query.where(LlmCall.purpose == purpose)
    with session_scope(read_only=True) as session:
        return [
            (
                row.purpose,
                CallDetails(
                    model=row.model,
                    prompt_hash=row.prompt_hash,
                    latency_ms=row.latency_ms,
                    input_tokens=row.input_tokens,
                    cached_input_tokens=row.cached_input_tokens,
                    output_tokens=row.output_tokens,
                    reasoning_tokens=row.reasoning_tokens,
                    request_id=row.request_id,
                    error=row.error,
                ),
            )
            for row in session.scalars(query.order_by(LlmCall.seq))
        ]
