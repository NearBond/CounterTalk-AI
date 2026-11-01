"""The speech-to-text interface (BRIEF D6; CODING_GUIDELINES §2.1).

Tests pass a fake, and another provider could be added later. A client sends one chunk and
returns the reply as it came, with the call's details; it never saves anything (the transcribe
handler does). parse() reads a reply, fresh or saved by an earlier try, into turns, so both go
through the same code.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Reply:
    """One chunk's reply, as it came, and the call's details."""

    body: dict[str, Any]
    # The provider's ID for the call, for its support.
    session_id: str | None
    wall_ms: int


@dataclass(frozen=True)
class ChunkTurn:
    """One turn within a chunk: times from the chunk's start, the speaker as the provider
    labelled them within this one request."""

    speaker: str
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class Parsed:
    turns: list[ChunkTurn]
    # The audio the provider says it billed, or None if the reply doesn't say.
    billed_ms: int | None


class SpeechToText(Protocol):
    provider: str
    model: str

    def transcribe(self, wav: bytes, languages: Sequence[str]) -> Reply:
        """One chunk, as 16-bit mono WAV. `languages` are ISO 639-1 codes to bias towards, or
        none. Raises PermanentError or RetryableError (§9.5)."""
        ...

    def parse(self, body: dict[str, Any], duration_ms: int) -> Parsed:
        """A reply's turns, never dropping text, with times inside the chunk's duration."""
        ...
