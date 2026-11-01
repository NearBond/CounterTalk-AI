"""Muse Voice Transcribe, through its file endpoint (BRIEF §8; CODING_GUIDELINES §2.1, §2.4).

One synchronous request per chunk: multipart, with a `request` part of settings and an `audio`
part of WAV. Each request carries a session ID of ours, so a failure can be traced with Meta's
support even when no reply came back. Retries are the job queue's: this sends once.
"""

import json
import logging
import math
import time
import uuid
from collections.abc import Sequence
from typing import Any

import httpx

# limits as a module, so a test can shorten one for everything here.
from countertalk import limits
from countertalk.errors import PermanentError, RetryableError
from countertalk.stt.base import ChunkTurn, Parsed, Reply

log = logging.getLogger(__name__)

URL = "https://api.meta.ai/v1/asr/transcribe"
MODEL = "muse-voice-transcribe-1.0"
# languageBias takes language names; the app passes ISO 639-1 codes.
LANGUAGE_NAMES = {"hi": "Hindi", "en": "English"}
# A turn whose speaker the reply doesn't give.
UNKNOWN_SPEAKER = "?"
# What each refusal means, from Meta's documentation.
REFUSALS = {
    400: "over 10 minutes, or not WAV",
    406: "a reply type it doesn't make",
    413: "over 32 MB",
}


class MetaSpeechToText:
    provider = "meta"
    model = MODEL

    def __init__(self, api_key: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self._api_key = api_key
        self._transport = transport

    def transcribe(self, wav: bytes, languages: Sequence[str]) -> Reply:
        if not self._api_key:
            # A server set up wrong: a retry works once the key is set.
            raise RetryableError(
                "stt_not_configured", "Speech-to-text isn't set up on the server (MODEL_API_KEY)"
            )
        session_id = str(uuid.uuid4())
        settings: dict[str, Any] = {"model": MODEL, "audioEncoding": "WAV", "mode": "DIARIZATION"}
        if languages:
            settings["languageBias"] = [LANGUAGE_NAMES[code] for code in languages]
        started = time.monotonic()
        try:
            with httpx.Client(
                transport=self._transport, timeout=limits.STT_TIMEOUT_MS / 1000
            ) as client:
                response = client.post(
                    URL,
                    params={"sessionId": session_id},
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Accept": "application/json",
                    },
                    files={
                        "request": (None, json.dumps(settings), "application/json"),
                        "audio": ("chunk.wav", wav, "audio/wav"),
                    },
                )
        except httpx.TimeoutException:
            seconds = limits.STT_TIMEOUT_MS // 1000
            raise RetryableError(
                "stt_timeout", f"Speech-to-text took over {seconds} s (session {session_id})"
            ) from None
        except httpx.HTTPError:
            raise RetryableError(
                "stt_network_error", f"Speech-to-text couldn't be reached (session {session_id})"
            ) from None
        wall_ms = round((time.monotonic() - started) * 1000)
        _check_status(response.status_code, session_id)
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise RetryableError(
                "stt_bad_reply",
                f"Speech-to-text's reply wasn't a JSON object (session {session_id})",
            )
        return Reply(body, _text(body.get("sessionId")) or session_id, wall_ms)

    def parse(self, body: dict[str, Any], duration_ms: int) -> Parsed:
        return parse(body, duration_ms)


def _check_status(status: int, session_id: str) -> None:
    """Classifies a refusal (§9.5). Only words of ours: Meta's own message isn't kept."""
    if 200 <= status < 300:
        return
    where = f"HTTP {status}, session {session_id}"
    if status in (401, 403):
        raise PermanentError(
            "stt_auth_failed", f"Speech-to-text refused the server's key ({where})"
        )
    if status == 429:
        raise RetryableError("stt_rate_limited", f"Speech-to-text is busy ({where})")
    if status >= 500 or status == 408:
        # One that keeps repeating ends as a dead job, with this session in its last error.
        raise RetryableError("stt_server_error", f"Speech-to-text failed ({where})")
    why = REFUSALS.get(status, "an answer the app doesn't expect")
    raise PermanentError("stt_refused", f"Speech-to-text refused the audio: {why} ({where})")


def parse(body: dict[str, Any], duration_ms: int) -> Parsed:
    """A reply's turns (§9.6). Every field may be missing, empty or of another type.

    - A turn's text is `transcript`, or `text` if that's all it has; one with neither is skipped.
    - A missing speaker is UNKNOWN_SPEAKER. A missing start follows on from the turn before; a
      missing or earlier end is the start. Times are kept inside the chunk.
    - A chunk whose turns hold less text than its transcript (none, say) becomes one turn, the
      whole chunk long, so no word is lost.
    """
    turns: list[ChunkTurn] = []
    raw_turns = body.get("turns")
    previous_end = 0
    for item in raw_turns if isinstance(raw_turns, list) else []:
        if not isinstance(item, dict):
            continue
        words = _text(item.get("transcript")) or _text(item.get("text"))
        if not words:
            continue
        start = _ms(item.get("startMs"))
        start = min(previous_end if start is None else start, duration_ms)
        end = _ms(item.get("endMs"))
        end = min(max(start if end is None else end, start), duration_ms)
        turns.append(ChunkTurn(_speaker(item.get("speaker")), start, end, words))
        previous_end = end
    whole = _text(body.get("transcript")) or ""
    in_turns = _letters(" ".join(t.text for t in turns))
    if len(in_turns) < len(_letters(whole)):
        # Meta's transcript is the turns' text joined, so turns holding less have lost words:
        # all of them are kept, as one turn whose speaker is unknown.
        if turns:
            log.warning(
                "stt turns hold less text than the transcript, kept as one turn turns=%d"
                " turn_chars=%d transcript_chars=%d",
                len(turns),
                len(in_turns),
                len(_letters(whole)),
            )
        turns = [ChunkTurn(UNKNOWN_SPEAKER, 0, duration_ms, whole)]
    return Parsed(turns, _ms(body.get("audioDurationMs")))


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _ms(value: object) -> int | None:
    """A time or length in ms, or None if it's missing, negative or not a finite number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return round(value) if value >= 0 else None


def _speaker(value: object) -> str:
    if isinstance(value, bool):
        return UNKNOWN_SPEAKER
    if isinstance(value, int):
        return str(value)
    return _text(value) or UNKNOWN_SPEAKER


def _letters(text: str) -> str:
    """Letters and digits only: the turns and the transcript may space and punctuate apart."""
    return "".join(c for c in text if c.isalnum())
