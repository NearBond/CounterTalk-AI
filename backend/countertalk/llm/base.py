"""The LLM interface (BRIEF §12 "Muse Spark"; CODING_GUIDELINES §2.1).

Tests pass a fake. A client sends messages and a JSON schema, and returns an Outcome: the
answer or the error, and the details of every request it made, failed ones too. It never saves
anything: the caller writes an LlmCall row from each call's details (§9.3), then raises the
error if there is one.
"""

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from countertalk.errors import PermanentError, RetryableError


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class CallDetails:
    """One request, for its LlmCall row. Never the prompt or the answer (§1.9): a hash of the
    request instead, so two runs can be told apart. Counts the reply didn't give are None."""

    model: str
    prompt_hash: str
    latency_ms: int
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    # As Meta counts them; reasoning tokens are billed as output (BRIEF §8).
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    # The provider's ID for the request, for its support.
    request_id: str | None = None
    # Why its answer wasn't used, as an error code; None when it was.
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class Outcome:
    calls: tuple[CallDetails, ...]
    # Matches the schema, when there's no error.
    answer: dict[str, Any] | None = None
    error: PermanentError | RetryableError | None = None


class StructuredLlm(Protocol):
    provider: str
    model: str

    def complete(
        self,
        messages: Sequence[Message],
        name: str,
        schema: dict[str, Any],
        *,
        claim_lost: threading.Event | None = None,
    ) -> Outcome:
        """An answer to `messages` that matches `schema` (strict JSON schema; its root an
        object), named `name` in the request. Never raises for a failed call: the Outcome
        carries the error, with every call made before it. A job's handler passes its
        `claim_lost`, checked before each paid request (§2.4). Raises ValueError, before
        sending anything, for a schema it can't check: a bug."""
        ...
