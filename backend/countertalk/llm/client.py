"""Muse Spark, through the OpenAI SDK (BRIEF §8, §12; CODING_GUIDELINES §2.1, §2.4).

Chat completions at Meta's address, with the key passed in (never OPENAI_API_KEY), the model
named, minimal reasoning, and a strict JSON schema the caller gives. The SDK retries 408, 409,
429, 5xx and network errors (max_retries=2); this adds the two retries of an answer's content:
an empty answer is asked again once, and one that doesn't match the schema gets one repair call
that says what's wrong. Every request's details come back in the Outcome, failed ones too.
"""

import hashlib
import json
import threading
import time
from collections.abc import Sequence
from typing import Any

import httpx2
import openai
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice

from countertalk import errors, limits
from countertalk.errors import PermanentError, RetryableError
from countertalk.llm.base import CallDetails, Message, Outcome
from countertalk.llm.schema import check_schema, problem
from countertalk.logs import provider_error

BASE_URL = "https://api.meta.ai/v1"
MODEL = "muse-spark-1.3"
# Meta's lowest: "none" is refused (checked 1 Nov). Roles and extraction need no long thought.
REASONING_EFFORT = "minimal"
REPAIR = (
    "That answer doesn't match the JSON schema: {problem}. Reply again with the whole answer, as"
    " JSON that matches the schema."
)
# Requests for one answer: the first, one asked again if empty, one repair.
MAX_REQUESTS = 3
GARBLED = "Muse Spark's reply was garbled"
NOT_OURS = {"OpenAI-Organization": openai.Omit(), "OpenAI-Project": openai.Omit()}


class MetaLlm:
    provider = "meta"
    model = MODEL

    def __init__(self, api_key: str, *, transport: httpx2.BaseTransport | None = None) -> None:
        self._client = None
        if api_key:
            self._client = openai.OpenAI(
                api_key=api_key,
                base_url=BASE_URL,
                timeout=limits.LLM_TIMEOUT_MS / 1000,
                max_retries=limits.LLM_MAX_RETRIES,
                http_client=httpx2.Client(transport=transport) if transport else None,
                # The SDK would send OPENAI_ORG_ID and OPENAI_PROJECT_ID from the environment:
                # another account's IDs, not Meta's business.
                default_headers=NOT_OURS,  # type: ignore[arg-type]
            )

    def complete(
        self,
        messages: Sequence[Message],
        name: str,
        schema: dict[str, Any],
        *,
        claim_lost: threading.Event | None = None,
    ) -> Outcome:
        if self._client is None:
            # A server set up wrong: a retry works once the key is set.
            error = RetryableError(
                "llm_not_configured", "Muse Spark isn't set up on the server (MODEL_API_KEY)"
            )
            return Outcome(calls=(), error=error)
        check_schema(schema)  # one that can't be checked is a bug: found before paying
        asked = [{"role": m.role, "content": m.content} for m in messages]
        sent = asked
        calls: list[CallDetails] = []
        asked_again = repaired = False
        for _ in range(MAX_REQUESTS):
            if claim_lost is not None and claim_lost.is_set():
                return Outcome(calls=tuple(calls), error=errors.claim_lost())
            request = {
                "model": MODEL,
                "reasoning_effort": REASONING_EFFORT,
                "messages": sent,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": name, "strict": True, "schema": schema},
                },
            }
            prompt_hash = _hash(request)
            started = time.monotonic()
            request_id: str | None = None
            tokens: dict[str, int | None] = {}
            try:
                raw = self._client.chat.completions.with_raw_response.create(**request)
                request_id = raw.headers.get("x-request-id")
                try:
                    completion = raw.parse()
                except ValueError:  # a 200 that says it's JSON, and isn't
                    raise RetryableError("llm_bad_response", GARBLED) from None
                # Counted even for an answer that can't be used: it was paid for.
                tokens = _tokens(getattr(completion, "usage", None))
                text = _text(completion)
            except openai.OpenAIError as sdk_error:
                failure = _classify(sdk_error)
                request_id = (failure.details or {}).get("request_id")
                calls.append(_details(prompt_hash, started, tokens, failure.code, request_id))
                return Outcome(calls=tuple(calls), error=failure)
            except (PermanentError, RetryableError) as failure:
                calls.append(_details(prompt_hash, started, tokens, failure.code, request_id))
                return Outcome(calls=tuple(calls), error=failure)
            if text is None or not text.strip():
                calls.append(_details(prompt_hash, started, tokens, "llm_empty_answer", request_id))
                if asked_again:
                    error = RetryableError("llm_bad_response", "Muse Spark gave an empty answer")
                    return Outcome(calls=tuple(calls), error=error)
                asked_again = True
                continue
            answer, wrong = _parse(text, schema)
            if wrong is None:
                calls.append(_details(prompt_hash, started, tokens, None, request_id))
                return Outcome(calls=tuple(calls), answer=answer)
            calls.append(_details(prompt_hash, started, tokens, "llm_answer_invalid", request_id))
            if repaired:
                break
            repaired = True
            sent = [
                *asked,
                {"role": "assistant", "content": text},
                {"role": "user", "content": REPAIR.format(problem=wrong)},
            ]
        error = PermanentError(
            "llm_answer_invalid", "Muse Spark's answer didn't match what was asked"
        )
        return Outcome(calls=tuple(calls), error=error)


def _hash(request: dict[str, Any]) -> str:
    text = json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def _details(
    prompt_hash: str,
    started: float,
    tokens: dict[str, int | None],
    error: str | None,
    request_id: str | None,
) -> CallDetails:
    latency_ms = round((time.monotonic() - started) * 1000)
    return CallDetails(MODEL, prompt_hash, latency_ms, **tokens, request_id=request_id, error=error)


def _text(completion: object) -> str | None:
    """The answer's text. Raises for a reply that isn't a usable completion: anything may come
    back in a 200, a string included (BRIEF §12)."""
    garbled = RetryableError("llm_bad_response", GARBLED)
    if not isinstance(completion, ChatCompletion):
        raise garbled
    choices = completion.choices
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Choice):
        raise garbled
    message = choices[0].message
    if not isinstance(message, ChatCompletionMessage):
        raise garbled
    refusal = message.refusal
    if (isinstance(refusal, str) and refusal.strip()) or choices[
        0
    ].finish_reason == "content_filter":
        raise PermanentError("llm_refused", "Muse Spark refused to answer")
    if choices[0].finish_reason == "length":
        raise PermanentError(
            "llm_answer_cut_off", "Muse Spark's answer was too long and was cut off"
        )
    if message.content is not None and not isinstance(message.content, str):
        raise garbled
    return message.content


def _tokens(usage: object) -> dict[str, int | None]:
    def count(owner: object, field: str) -> int | None:
        value = getattr(owner, field, None)
        return (
            value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        )

    return {
        "input_tokens": count(usage, "prompt_tokens"),
        "cached_input_tokens": count(
            getattr(usage, "prompt_tokens_details", None), "cached_tokens"
        ),
        "output_tokens": count(usage, "completion_tokens"),
        "reasoning_tokens": count(
            getattr(usage, "completion_tokens_details", None), "reasoning_tokens"
        ),
    }


def _parse(text: str, schema: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """The answer, or what's wrong with it, in words for the repair call."""

    def refuse_constant(name: str) -> object:
        raise ValueError(name)  # NaN and Infinity aren't JSON

    try:
        value = json.loads(text, parse_constant=refuse_constant)
    except (ValueError, RecursionError):  # RecursionError: nested too deep to parse
        return None, "it isn't JSON"
    try:
        wrong = problem(value, schema)
    except RecursionError:  # a schema whose $defs refer to themselves, and a deep answer
        return None, "it's nested too deeply"

    if wrong is not None or not isinstance(value, dict):
        return None, wrong or "it isn't a JSON object"
    return value, None


def _classify(error: openai.OpenAIError) -> PermanentError | RetryableError:
    """§9.5. Only words of ours: Meta's message for a 4xx could quote the prompt (§1.9), so only
    its error type is kept, in details, for the log."""
    if isinstance(error, openai.APITimeoutError):
        seconds = limits.LLM_TIMEOUT_MS // 1000
        return RetryableError("llm_unreachable", f"Muse Spark took over {seconds} s")
    if isinstance(error, openai.APIConnectionError):
        return RetryableError("llm_unreachable", "Muse Spark couldn't be reached")
    if not isinstance(error, openai.APIStatusError):
        return RetryableError("llm_bad_response", GARBLED)
    status = error.status_code
    found: dict[str, Any] = {"http_status": status, "request_id": error.request_id}
    if 400 <= status < 500:
        found |= provider_error(error.body, ("type",))  # the SDK's body is Meta's error object
    if status in (401, 403):
        return PermanentError("llm_auth_failed", "Muse Spark refused the server's key", found)
    if status in (408, 409, 429) or status >= 500:
        return RetryableError("llm_unavailable", "Muse Spark is busy", found)
    if 400 <= status < 500:
        return PermanentError("llm_rejected", "Muse Spark couldn't accept this request", found)
    return RetryableError("llm_bad_response", GARBLED, found)
