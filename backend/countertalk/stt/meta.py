"""Muse Voice Transcribe, through its file endpoint (BRIEF §8; CODING_GUIDELINES §2.1, §2.4).

One synchronous request per chunk: multipart, with a `request` part of settings and an `audio`
part of WAV. Each request carries a session ID of ours, so a failure can be traced with Meta's
support even when no reply came back: every error carries it in `details`. Retries are the job
queue's: this sends once.
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
from countertalk.logs import provider_error
from countertalk.stt.base import ChunkTurn, Parsed, Reply

log = logging.getLogger(__name__)

URL = "https://api.meta.ai/v1/asr/transcribe"
MODEL = "muse-voice-transcribe-1.0"
# languageBias takes language names; the app passes ISO 639-1 codes.
LANGUAGE_NAMES = {"hi": "Hindi", "en": "English"}
# A turn whose speaker the reply doesn't give.
UNKNOWN_SPEAKER = "?"
# What each refusal tells the rep. Meta's 400 is a file over 10 minutes or not WAV, but also a
# request it won't take (a keyword over 30 characters, 1 Nov), so it names neither.
REFUSALS = {
    400: "Speech-to-text couldn't accept this request",
    406: "Speech-to-text refused the recording: a reply type it doesn't make",
    413: "Speech-to-text refused the recording: over 32 MB",
}
UNEXPECTED_REFUSAL = "Speech-to-text refused the recording: an answer the app doesn't expect"


class MetaSpeechToText:
    provider = "meta"
    model = MODEL

    def __init__(self, api_key: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self._api_key = api_key
        self._transport = transport

    def transcribe(self, wav: bytes, languages: Sequence[str], keywords: Sequence[str]) -> Reply:
        if not self._api_key:
            # A server set up wrong: a retry works once the key is set.
            raise RetryableError(
                "stt_not_configured", "Speech-to-text isn't set up on the server (MODEL_API_KEY)"
            )
        session_id = str(uuid.uuid4())
        settings: dict[str, Any] = {"model": MODEL, "audioEncoding": "WAV", "mode": "DIARIZATION"}
        if languages:
            settings["languageBias"] = [LANGUAGE_NAMES[code] for code in languages]
        if keywords:
            # Meta refuses the request for one over 30 characters (undocumented): the caller
            # leaves those out, and caps how many (STT_KEYWORD_MAX_CHARS, STT_KEYWORDS_MAX).
            settings["keywords"] = list(keywords)
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
                "stt_timeout",
                f"Speech-to-text took over {seconds} s",
                details={"session_id": session_id},
            ) from None
        except httpx.HTTPError:
            raise RetryableError(
                "stt_network_error",
                "Speech-to-text couldn't be reached",
                details={"session_id": session_id},
            ) from None
        wall_ms = round((time.monotonic() - started) * 1000)
        found: dict[str, Any] = {"session_id": session_id, "http_status": response.status_code}
        if 400 <= response.status_code < 500:
            found |= _metas_error(response, with_message=response.status_code not in (401, 403))
        _check_status(response.status_code, found)
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise RetryableError(
                "stt_bad_reply", "Speech-to-text's reply wasn't a JSON object", details=found
            )
        return Reply(body, _text(body.get("sessionId")) or session_id, wall_ms)

    def parse(self, body: dict[str, Any], duration_ms: int) -> Parsed:
        return parse(body, duration_ms)


def _metas_error(response: httpx.Response, *, with_message: bool) -> dict[str, str]:
    """A 4xx's `error.type` and `error.message`, for the log only: they're about the request,
    not the audio (R5). Strings only, on one line, at most PROVIDER_ERROR_TEXT_MAX characters.

    A refused key's message may echo part of the key (§1.9), so for one only the type is kept.
    """
    try:
        body = response.json()
    except ValueError:
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    return provider_error(error, ("type", "message") if with_message else ("type",))


def _check_status(status: int, found: dict[str, Any]) -> None:
    """Classifies a refusal (§9.5). The message is only words of ours.

    A permanent error's message becomes the visit's failure reason, which the rep reads, so
    messages are plain words. The session, the status and, for a 4xx, Meta's own error type and
    message are in `details`, which the transcribe handler logs with the visit, for Meta's support
    if a failure repeats (BRIEF §8).
    """
    if 200 <= status < 300:
        return
    if status in (401, 403):
        raise PermanentError(
            "stt_auth_failed",
            "Speech-to-text refused the server's key",
            details=found,
        )
    if status == 429:
        raise RetryableError("stt_rate_limited", "Speech-to-text is busy", details=found)
    if status >= 500 or status == 408:
        raise RetryableError("stt_server_error", "Speech-to-text failed", details=found)
    raise PermanentError("stt_refused", REFUSALS.get(status, UNEXPECTED_REFUSAL), details=found)


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
