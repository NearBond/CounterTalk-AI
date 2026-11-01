"""llm/client.py: Muse Spark through the OpenAI SDK, its errors and its repairs (batch 9).

No network: httpx2's MockTransport answers in place of Meta, under the real SDK (§3.2), so its
retries run too. A retried answer says to wait 1 ms (retry-after-ms) to keep the tests fast.
"""

import json
import logging
import re
import threading
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from countertalk import limits
from countertalk.errors import PermanentError, RetryableError
from countertalk.llm import client as llm_client
from countertalk.llm.base import CallDetails, Message

KEY = "not-a-real-key"
SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string"}, "score": {"type": ["number", "null"]}},
    "required": ["label", "score"],
    "additionalProperties": False,
}
MESSAGES = [Message("system", "Answer in JSON."), Message("user", "Who spoke first, Asha?")]
USAGE = {
    "prompt_tokens": 120,
    "completion_tokens": 30,
    "total_tokens": 150,
    "prompt_tokens_details": {"cached_tokens": 100},
    "completion_tokens_details": {"reasoning_tokens": 12},
}
QUICK_RETRY = {"retry-after-ms": "1"}

Answer = httpx2.Response | Exception


def completion(
    content: str | None = '{"label": "c0:A", "score": 0.5}',
    *,
    finish_reason: str = "stop",
    refusal: str | None = None,
    usage: object = USAGE,
) -> httpx2.Response:
    body = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": llm_client.MODEL,
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content, "refusal": refusal},
            }
        ],
        "usage": usage,
    }
    return httpx2.Response(200, json=body, headers={"x-request-id": "req-1"})


class Meta:
    """Answers each request with the next of `answers`, or the last one again once they run out
    (the SDK's retries get the same answer)."""

    def __init__(self, *answers: Answer | Callable[[], Answer]) -> None:
        self.answers = list(answers)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        answer = self.answers[min(len(self.requests), len(self.answers)) - 1]
        answer = answer() if callable(answer) and not isinstance(answer, Exception) else answer
        if isinstance(answer, Exception):
            raise answer
        # A fresh copy each time: a response's body can be read only once.
        return httpx2.Response(answer.status_code, headers=answer.headers, content=answer.content)

    def sent(self, index: int) -> dict[str, Any]:
        return json.loads(self.requests[index].content)

    def llm(self, key: str = KEY) -> llm_client.MetaLlm:
        return llm_client.MetaLlm(key, transport=httpx2.MockTransport(self))


def test_a_request_carries_metas_settings_and_the_schema_its_given() -> None:
    meta = Meta(completion())

    outcome = meta.llm().complete(MESSAGES, "speaker_roles", SCHEMA)

    assert outcome.error is None
    assert outcome.answer == {"label": "c0:A", "score": 0.5}
    [request] = meta.requests
    assert (request.method, str(request.url)) == ("POST", f"{llm_client.BASE_URL}/chat/completions")
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.extensions["timeout"]["read"] == limits.LLM_TIMEOUT_MS / 1000
    assert meta.sent(0) == {
        "model": "muse-spark-1.3",
        "reasoning_effort": "minimal",
        "messages": [
            {"role": "system", "content": "Answer in JSON."},
            {"role": "user", "content": "Who spoke first, Asha?"},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "speaker_roles", "strict": True, "schema": SCHEMA},
        },
    }
    [call] = outcome.calls
    assert re.fullmatch("[0-9a-f]{64}", call.prompt_hash)
    assert call.latency_ms >= 0
    assert call == CallDetails(
        model="muse-spark-1.3",
        prompt_hash=call.prompt_hash,
        latency_ms=call.latency_ms,
        input_tokens=120,
        cached_input_tokens=100,
        output_tokens=30,
        reasoning_tokens=12,
        request_id="req-1",
        error=None,
    )
    assert call.ok


def test_the_key_and_address_are_ours_whatever_the_environment_says(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    # Another account's IDs, which the SDK would send as headers.
    monkeypatch.setenv("OPENAI_ORG_ID", "org-elsewhere")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj-elsewhere")
    meta = Meta(completion())

    meta.llm().complete(MESSAGES, "speaker_roles", SCHEMA)

    [request] = meta.requests
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.url.host == "api.meta.ai"
    assert [name for name in request.headers if name.startswith("openai-")] == []


def test_the_same_request_has_the_same_prompt_hash_and_another_a_different_one() -> None:
    meta = Meta(completion())
    llm = meta.llm()

    first = llm.complete(MESSAGES, "speaker_roles", SCHEMA).calls[0].prompt_hash
    again = llm.complete(MESSAGES, "speaker_roles", SCHEMA).calls[0].prompt_hash
    other = llm.complete(MESSAGES[:1], "speaker_roles", SCHEMA).calls[0].prompt_hash

    assert first == again != other


def test_a_429_is_retried_by_the_sdk_and_logged_as_one_call() -> None:
    meta = Meta(
        httpx2.Response(429, json={"error": {"message": "slow"}}, headers=QUICK_RETRY), completion()
    )

    outcome = meta.llm().complete(MESSAGES, "speaker_roles", SCHEMA)

    assert outcome.answer == {"label": "c0:A", "score": 0.5}
    assert len(meta.requests) == 2
    assert [call.error for call in outcome.calls] == [None]


def refused(status: int) -> httpx2.Response:
    body = {"error": {"type": "invalid_request_error", "message": "Asha's words were refused"}}
    return httpx2.Response(status, json=body, headers={**QUICK_RETRY, "x-request-id": "req-9"})


def garbled(body: object) -> Callable[[], httpx2.Response]:
    return lambda: httpx2.Response(200, json=body)


TRIES = limits.LLM_MAX_RETRIES + 1  # what the SDK retries, it tries this many times
KEY_REFUSED = "Muse Spark refused the server's key"
REJECTED = "Muse Spark couldn't accept this request"
BUSY = "Muse Spark is busy"
GARBLED = "Muse Spark's reply was garbled"
REFUSED = "Muse Spark refused to answer"


@pytest.mark.parametrize(
    ("answer", "kind", "code", "words", "requests"),
    [
        (refused(401), PermanentError, "llm_auth_failed", KEY_REFUSED, 1),
        (refused(403), PermanentError, "llm_auth_failed", KEY_REFUSED, 1),
        (refused(400), PermanentError, "llm_rejected", REJECTED, 1),
        (refused(404), PermanentError, "llm_rejected", REJECTED, 1),
        (refused(422), PermanentError, "llm_rejected", REJECTED, 1),
        (refused(408), RetryableError, "llm_unavailable", BUSY, TRIES),
        (refused(409), RetryableError, "llm_unavailable", BUSY, TRIES),
        (refused(429), RetryableError, "llm_unavailable", BUSY, TRIES),
        (refused(500), RetryableError, "llm_unavailable", BUSY, TRIES),
        (refused(503), RetryableError, "llm_unavailable", BUSY, TRIES),
        (
            httpx2.ConnectError("refused"),
            RetryableError,
            "llm_unreachable",
            "Muse Spark couldn't be reached",
            TRIES,
        ),
        (
            httpx2.ReadTimeout("slow"),
            RetryableError,
            "llm_unreachable",
            f"Muse Spark took over {limits.LLM_TIMEOUT_MS // 1000} s",
            TRIES,
        ),
        (garbled("a string, not a completion"), RetryableError, "llm_bad_response", GARBLED, 1),
        (garbled(["a", "list"]), RetryableError, "llm_bad_response", GARBLED, 1),
        (
            lambda: httpx2.Response(200, text="<html>proxy</html>"),
            RetryableError,
            "llm_bad_response",
            GARBLED,
            1,
        ),
        (
            lambda: httpx2.Response(
                200, content=b"{not json", headers={"content-type": "application/json"}
            ),
            RetryableError,
            "llm_bad_response",
            GARBLED,
            1,
        ),
        (garbled({"id": "no choices"}), RetryableError, "llm_bad_response", GARBLED, 1),
        (garbled({"choices": []}), RetryableError, "llm_bad_response", GARBLED, 1),
        (
            garbled({"choices": [{"message": "text"}]}),
            RetryableError,
            "llm_bad_response",
            GARBLED,
            1,
        ),
        (lambda: completion(content=7), RetryableError, "llm_bad_response", GARBLED, 1),  # type: ignore[arg-type]
        (lambda: completion(None, refusal="I can't"), PermanentError, "llm_refused", REFUSED, 1),
        (
            lambda: completion(finish_reason="content_filter"),
            PermanentError,
            "llm_refused",
            REFUSED,
            1,
        ),
        (
            lambda: completion('{"label": "c0', finish_reason="length"),
            PermanentError,
            "llm_answer_cut_off",
            "Muse Spark's answer was too long and was cut off",
            1,
        ),
    ],
)
def test_each_failure_is_classified_and_logged_as_a_failed_call(
    answer: Answer | Callable[[], Answer],
    kind: type[Exception],
    code: str,
    words: str,
    requests: int,
) -> None:
    meta = Meta(answer)

    outcome = meta.llm().complete(MESSAGES, "speaker_roles", SCHEMA)

    assert type(outcome.error) is kind
    assert outcome.error is not None
    # Plain words for the rep: never the status, nor Meta's own message.
    assert (outcome.error.code, outcome.error.message) == (code, words)
    assert outcome.answer is None
    assert len(meta.requests) == requests  # the SDK retried what it retries, and only that
    assert [call.error for call in outcome.calls] == [code]


def test_a_4xx_keeps_metas_error_type_never_its_message() -> None:
    # A 400 about the prompt could quote the transcript (§1.9).
    outcome = Meta(refused(400)).llm().complete(MESSAGES, "speaker_roles", SCHEMA)

    assert outcome.error is not None
    assert outcome.error.details == {
        "http_status": 400,
        "request_id": "req-9",
        "error_type": "invalid_request_error",
    }
    assert outcome.calls[0].request_id == "req-9"


@pytest.mark.parametrize(
    "answer",
    [completion(None, refusal="I can't help with that"), completion("{", finish_reason="length")],
    ids=["refused", "cut-off"],
)
def test_an_answer_that_cant_be_used_still_counts_its_tokens(answer: httpx2.Response) -> None:
    # It was paid for (§9.3).
    outcome = Meta(answer).llm().complete(MESSAGES, "speaker_roles", SCHEMA)

    [call] = outcome.calls
    assert (call.input_tokens, call.output_tokens, call.request_id) == (120, 30, "req-1")


@pytest.mark.parametrize(
    ("label", "why"),
    [
        ({"type": "string", "pattern": "^c"}, "can't check the schema keyword pattern"),
        ({"type": "strng"}, "unknown type strng"),
        ({"$ref": "#/$defs/nowhere"}, "the schema has no $defs entry for #/$defs/nowhere"),
        ({"$ref": "other.json#/label"}, "the schema has no $defs entry for other.json#/label"),
    ],
)
def test_a_schema_it_cant_check_fails_before_anything_is_paid_for(
    label: dict[str, Any], why: str
) -> None:
    # A bug of ours: raised, not an Outcome, and before a request is sent.
    meta = Meta(completion())
    schema = {**SCHEMA, "properties": {**SCHEMA["properties"], "label": label}}

    with pytest.raises(ValueError) as error:
        meta.llm().complete(MESSAGES, "speaker_roles", schema)

    assert (str(error.value), meta.requests) == (why, [])


def test_a_lost_claim_stops_before_the_next_paid_request() -> None:
    # A job's handler pays per call (§2.4): once another worker may have the job, it stops.
    claim_lost = threading.Event()

    def invalid_then_lose_the_claim() -> httpx2.Response:
        claim_lost.set()
        return completion("not json")

    meta = Meta(invalid_then_lose_the_claim, completion())

    outcome = meta.llm().complete(MESSAGES, "speaker_roles", SCHEMA, claim_lost=claim_lost)

    assert len(meta.requests) == 1  # no repair call
    assert outcome.error is not None and outcome.error.code == "claim_lost"
    assert [call.error for call in outcome.calls] == ["llm_answer_invalid"]


def test_a_claim_lost_before_the_first_request_sends_nothing() -> None:
    claim_lost = threading.Event()
    claim_lost.set()
    meta = Meta(completion())

    outcome = meta.llm().complete(MESSAGES, "speaker_roles", SCHEMA, claim_lost=claim_lost)

    assert outcome.error is not None and outcome.error.code == "claim_lost"
    assert (meta.requests, outcome.calls) == ([], ())


@pytest.mark.parametrize("usage", [None, "lots", {"prompt_tokens": True, "completion_tokens": -1}])
def test_odd_token_counts_are_absent_not_a_failure(usage: object) -> None:
    outcome = Meta(completion(usage=usage)).llm().complete(MESSAGES, "speaker_roles", SCHEMA)

    [call] = outcome.calls
    assert outcome.answer == {"label": "c0:A", "score": 0.5}
    assert (call.input_tokens, call.cached_input_tokens) == (None, None)
    assert (call.output_tokens, call.reasoning_tokens) == (None, None)


@pytest.mark.parametrize("empty", ["", "  \n", None])
def test_an_empty_answer_is_asked_again(empty: str | None) -> None:
    meta = Meta(completion(empty), completion())

    outcome = meta.llm().complete(MESSAGES, "speaker_roles", SCHEMA)

    assert outcome.answer == {"label": "c0:A", "score": 0.5}
    assert meta.sent(1)["messages"] == meta.sent(0)["messages"]
    assert [call.error for call in outcome.calls] == ["llm_empty_answer", None]


def test_an_empty_answer_twice_fails_to_be_retried_later() -> None:
    outcome = Meta(completion(""), completion("")).llm().complete(MESSAGES, "speaker_roles", SCHEMA)

    assert type(outcome.error) is RetryableError
    assert outcome.error.code == "llm_bad_response"
    assert [call.error for call in outcome.calls] == ["llm_empty_answer", "llm_empty_answer"]


@pytest.mark.parametrize(
    ("bad", "why"),
    [
        ('{"label": 5, "score": null}', "label: should be string, not integer"),
        ("Sure! Here it is.", "it isn't JSON"),
        ('{"label": "c0:A", "score": NaN}', "it isn't JSON"),
        ("[" * 100_000 + "]" * 100_000, "it isn't JSON"),  # too deep for the parser
    ],
)
def test_an_answer_that_doesnt_match_gets_one_repair_call_with_the_error(
    bad: str, why: str
) -> None:
    meta = Meta(completion(bad), completion())

    outcome = meta.llm().complete(MESSAGES, "speaker_roles", SCHEMA)

    assert outcome.answer == {"label": "c0:A", "score": 0.5}
    assert meta.sent(1)["messages"] == [
        *meta.sent(0)["messages"],
        {"role": "assistant", "content": bad},
        {"role": "user", "content": llm_client.REPAIR.format(problem=why)},
    ]
    assert [call.error for call in outcome.calls] == ["llm_answer_invalid", None]
    assert outcome.calls[0].prompt_hash != outcome.calls[1].prompt_hash


def test_an_answer_still_wrong_after_the_repair_fails_for_good() -> None:
    meta = Meta(completion('{"label": 5, "score": null}'), completion("[]"))

    outcome = meta.llm().complete(MESSAGES, "speaker_roles", SCHEMA)

    assert type(outcome.error) is PermanentError
    assert outcome.error.code == "llm_answer_invalid"
    assert len(meta.requests) == 2
    assert [call.error for call in outcome.calls] == ["llm_answer_invalid", "llm_answer_invalid"]


def test_a_failure_during_the_repair_keeps_both_calls() -> None:
    meta = Meta(completion("not json"), refused(401))

    outcome = meta.llm().complete(MESSAGES, "speaker_roles", SCHEMA)

    assert outcome.error is not None and outcome.error.code == "llm_auth_failed"
    assert [call.error for call in outcome.calls] == ["llm_answer_invalid", "llm_auth_failed"]


def test_without_a_key_nothing_is_sent() -> None:
    meta = Meta(completion())

    outcome = meta.llm(key="").complete(MESSAGES, "speaker_roles", SCHEMA)

    assert type(outcome.error) is RetryableError
    assert outcome.error.code == "llm_not_configured"
    assert (meta.requests, outcome.calls) == ([], ())


def test_prompts_and_answers_are_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    secret_answer = '{"label": "Asha-from-the-answer", "score": null}'
    meta = Meta(completion("not json from Asha"), completion(secret_answer), refused(400))

    with caplog.at_level(logging.DEBUG):
        meta.llm().complete(MESSAGES, "speaker_roles", SCHEMA)
        meta.llm().complete(MESSAGES, "speaker_roles", SCHEMA)

    assert caplog.records  # the SDK and httpx2 did log
    assert "Asha" not in caplog.text and "Answer in JSON" not in caplog.text
