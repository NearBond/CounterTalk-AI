"""stt/meta.py: Muse Voice Transcribe's request, its errors, and reading its replies (batch 6).

No network: httpx's MockTransport answers in place of Meta (§3.2).
"""

import json
import logging
from collections.abc import Callable

import httpx
import pytest

from countertalk import limits
from countertalk.errors import PermanentError, RetryableError
from countertalk.stt import meta
from countertalk.stt.base import ChunkTurn

KEY = "not-a-real-key"
WAV = b"RIFF fake chunk bytes"
REPLY = {
    "sessionId": "meta-session-1",
    "transcript": "Namaste ji. Haan bolo.",
    "audioDurationMs": 4_700,
    "turns": [
        {"turnId": 1, "speaker": "A", "startMs": 100, "endMs": 1_500, "transcript": "Namaste ji."},
        {"turnId": 2, "speaker": "B", "startMs": 2_000, "endMs": 4_600, "transcript": "Haan bolo."},
    ],
}


def client(answer: Callable[[httpx.Request], httpx.Response]) -> meta.MetaSpeechToText:
    return meta.MetaSpeechToText(KEY, transport=httpx.MockTransport(answer))


def parts(request: httpx.Request) -> dict[str, tuple[dict[str, str], bytes]]:
    """Each multipart part's headers and body, by its name."""
    boundary = request.headers["content-type"].split("boundary=")[1].encode()
    found = {}
    for piece in request.read().split(b"--" + boundary)[1:-1]:
        head, _, body = piece.strip(b"\r\n").partition(b"\r\n\r\n")
        headers = dict(line.split(": ", 1) for line in head.decode().split("\r\n") if ": " in line)
        name = headers["Content-Disposition"].split('name="')[1].split('"')[0]
        found[name] = (headers, body)
    return found


@pytest.mark.parametrize(
    ("languages", "bias", "keywords"),
    [(("hi", "en"), ["Hindi", "English"], ("Kurkure", "Lay's")), ((), None, ())],
    ids=["bias-and-keywords", "neither"],
)
def test_a_request_sends_the_chunk_with_metas_settings(
    languages: tuple[str, ...], bias: list[str] | None, keywords: tuple[str, ...]
) -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=REPLY)

    client(answer).transcribe(WAV, languages, keywords)

    [request] = seen
    assert (request.method, request.url.copy_with(query=None)) == ("POST", meta.URL)
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["accept"] == "application/json"
    assert request.url.params["sessionId"]  # ours, so it can be logged before the call
    assert request.extensions["timeout"]["read"] == limits.STT_TIMEOUT_MS / 1000
    sent = parts(request)
    settings_headers, settings = sent["request"]
    assert settings_headers["Content-Type"] == "application/json"
    expected = {"model": meta.MODEL, "audioEncoding": "WAV", "mode": "DIARIZATION"}
    expected |= {"languageBias": bias} if bias else {}
    expected |= {"keywords": list(keywords)} if keywords else {}
    assert json.loads(settings) == expected
    audio_headers, audio = sent["audio"]
    assert (audio_headers["Content-Type"], audio) == ("audio/wav", WAV)


def test_a_reply_comes_back_as_it_came_with_its_session_and_time() -> None:
    reply = client(lambda request: httpx.Response(200, json=REPLY)).transcribe(WAV, (), ())

    assert reply.body == REPLY
    assert reply.session_id == "meta-session-1"
    assert reply.wall_ms >= 0


def refuse(status: int) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(status, json={"error": {"message": "no"}})


def raise_(error: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def answer(request: httpx.Request) -> httpx.Response:
        raise error

    return answer


@pytest.mark.parametrize(
    ("answer", "kind", "code"),
    [
        (refuse(401), PermanentError, "stt_auth_failed"),
        (refuse(403), PermanentError, "stt_auth_failed"),
        (refuse(400), PermanentError, "stt_refused"),
        (refuse(406), PermanentError, "stt_refused"),
        (refuse(413), PermanentError, "stt_refused"),
        (refuse(404), PermanentError, "stt_refused"),
        (refuse(429), RetryableError, "stt_rate_limited"),
        (refuse(500), RetryableError, "stt_server_error"),
        (refuse(503), RetryableError, "stt_server_error"),
        (
            lambda request: httpx.Response(200, text="<html>proxy</html>"),
            RetryableError,
            "stt_bad_reply",
        ),
        (
            lambda request: httpx.Response(200, json=["not", "an", "object"]),
            RetryableError,
            "stt_bad_reply",
        ),
        (raise_(httpx.ConnectError("refused")), RetryableError, "stt_network_error"),
        (raise_(httpx.ReadTimeout("slow")), RetryableError, "stt_timeout"),
    ],
)
def test_each_failure_is_classified_and_carries_its_session(
    answer: Callable[[httpx.Request], httpx.Response], kind: type[Exception], code: str
) -> None:
    sessions: list[str] = []
    statuses: list[int] = []

    def answer_and_note(request: httpx.Request) -> httpx.Response:
        sessions.append(request.url.params["sessionId"])
        response = answer(request)
        statuses.append(response.status_code)
        return response

    with pytest.raises(kind) as error:
        client(answer_and_note).transcribe(WAV, (), ())

    failure = error.value
    assert isinstance(failure, PermanentError | RetryableError)
    assert failure.code == code
    # For Meta's support, when a failure repeats (BRIEF §8): the handler logs them.
    found = failure.details or {}
    assert found["session_id"] == sessions[0]
    assert found.get("http_status") == (statuses[0] if statuses else None)
    # A permanent error's message is the visit's failure reason: plain words for the rep.
    assert sessions[0] not in failure.message and "HTTP" not in failure.message


def test_a_400_tells_the_rep_neither_too_long_nor_not_wav() -> None:
    # Meta's 400 also covers a request it won't take, such as a keyword over 30 characters.
    with pytest.raises(PermanentError) as error:
        client(refuse(400)).transcribe(WAV, (), ())

    assert error.value.message == "Speech-to-text couldn't accept this request"


CUT = limits.STT_ERROR_TEXT_MAX


@pytest.mark.parametrize(
    ("status", "body", "kept"),
    [
        (
            400,
            {"error": {"type": "invalid_request_error", "message": "ASR keyword 39 is too long"}},
            {"error_type": "invalid_request_error", "error_message": "ASR keyword 39 is too long"},
        ),
        (
            404,
            {"error": {"type": "not_found", "message": "no\nsuch\r\n path"}},
            {"error_type": "not_found", "error_message": "no such path"},
        ),
        (429, {"error": {"message": "y" * (CUT + 1)}}, {"error_message": "y" * CUT}),
        # A refused key's message may echo part of it (§1.9): its type only.
        (401, {"error": {"type": "auth", "message": "Bad key ab12...yz"}}, {"error_type": "auth"}),
        (403, {"error": {"type": "denied", "message": "Key ab12...yz"}}, {"error_type": "denied"}),
        (413, {"error": {"type": 7, "message": ["no"]}}, {}),
        (406, {"error": "only words"}, {}),
        (404, ["not", "an", "object"], {}),
        (500, {"error": {"type": "server_error", "message": "down"}}, {}),
    ],
    ids=[
        "400",
        "one-line",
        "cut",
        "401-type-only",
        "403-type-only",
        "not-strings",
        "not-an-object",
        "not-json-object",
        "5xx-not-kept",
    ],
)
def test_a_4xx_keeps_metas_error_type_and_message_for_the_log(
    status: int, body: object, kept: dict[str, str]
) -> None:
    # They're about the request, not the audio, so safe to log (R5); never in the rep's reason.
    with pytest.raises((PermanentError, RetryableError)) as error:
        client(lambda request: httpx.Response(status, json=body)).transcribe(WAV, (), ())

    found = error.value.details or {}
    assert {key: found[key] for key in found if key.startswith("error_")} == kept
    assert all(text not in error.value.message for text in kept.values())


def test_a_4xx_body_that_isnt_json_keeps_nothing() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="<html>bad request</html>")

    with pytest.raises(PermanentError) as error:
        client(answer).transcribe(WAV, (), ())

    assert set(error.value.details or {}) == {"session_id", "http_status"}


def test_without_a_key_nothing_is_sent() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        raise AssertionError("sent a request with no key")

    stt = meta.MetaSpeechToText("", transport=httpx.MockTransport(answer))

    with pytest.raises(RetryableError) as error:
        stt.transcribe(WAV, (), ())

    assert error.value.code == "stt_not_configured"


# --- reading a reply ---------------------------------------------------------------------------
CHUNK_MS = 10_000
A_TURN = {"speaker": "A", "startMs": 100, "endMs": 900, "transcript": "Namaste ji."}
BOTH_TURNS = [("A", 100, 1_500, "Namaste ji."), ("B", 2_000, 4_600, "Haan bolo.")]


@pytest.mark.parametrize(
    ("body", "turns", "billed_ms"),
    [
        (REPLY, BOTH_TURNS, 4_700),
        # Spaced and punctuated apart, or holding more than the transcript: the turns stand.
        ({**REPLY, "transcript": "namaste JI, haan bolo!"}, BOTH_TURNS, 4_700),
        ({**REPLY, "transcript": "Namaste ji."}, BOTH_TURNS, 4_700),
        # The text under another name is still kept.
        (
            {"turns": [{**A_TURN, "transcript": None, "text": "Namaste."}]},
            [("A", 100, 900, "Namaste.")],
            None,
        ),
        ({"turns": [{**A_TURN, "speaker": None}]}, [("?", 100, 900, "Namaste ji.")], None),
        ({"turns": [{**A_TURN, "speaker": 0}]}, [("0", 100, 900, "Namaste ji.")], None),
        # Missing times follow on from the turn before.
        (
            {"turns": [A_TURN, {"speaker": "B", "transcript": "Bolo."}]},
            [("A", 100, 900, "Namaste ji."), ("B", 900, 900, "Bolo.")],
            None,
        ),
        ({"turns": [{**A_TURN, "endMs": 50}]}, [("A", 100, 100, "Namaste ji.")], None),
        (
            {"turns": [{**A_TURN, "startMs": -5, "endMs": 99_000}]},
            [("A", 0, CHUNK_MS, "Namaste ji.")],
            None,
        ),
        ({"turns": [{**A_TURN, "startMs": "100"}]}, [("A", 0, 900, "Namaste ji.")], None),
        ({"turns": [{**A_TURN, "transcript": "  "}, "junk", None]}, [], None),
        # A chunk with text but no turns becomes one turn.
        ({"transcript": "Sab theek."}, [("?", 0, CHUNK_MS, "Sab theek.")], None),
        ({"transcript": "Sab theek.", "turns": []}, [("?", 0, CHUNK_MS, "Sab theek.")], None),
        ({"transcript": "Sab theek.", "turns": None}, [("?", 0, CHUNK_MS, "Sab theek.")], None),
        (
            {"transcript": "Sab theek.", "turns": [{"speaker": "A"}]},
            [("?", 0, CHUNK_MS, "Sab theek.")],
            None,
        ),
        ({}, [], None),
        ({"transcript": "", "turns": [], "audioDurationMs": 3_000}, [], 3_000),
        ({"audioDurationMs": "3000"}, [], None),
        ({"audioDurationMs": -1}, [], None),
    ],
)
def test_a_reply_of_any_shape_keeps_its_text(
    body: dict, turns: list[tuple[str, int, int, str]], billed_ms: int | None
) -> None:
    parsed = meta.parse(body, CHUNK_MS)

    assert parsed.turns == [ChunkTurn(*turn) for turn in turns]
    assert parsed.billed_ms == billed_ms


def test_turns_that_lose_words_give_way_to_the_whole_transcript(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = {**REPLY, "turns": REPLY["turns"][:1]}

    with caplog.at_level(logging.WARNING):
        parsed = meta.parse(body, CHUNK_MS)

    assert parsed.turns == [ChunkTurn("?", 0, CHUNK_MS, "Namaste ji. Haan bolo.")]
    [record] = [r for r in caplog.records if r.name == "countertalk.stt.meta"]
    assert "Haan" not in record.getMessage() and "Namaste" not in record.getMessage()
