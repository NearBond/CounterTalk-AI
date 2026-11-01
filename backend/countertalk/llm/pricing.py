"""Muse Spark's prices, and what a request costs before and after it's sent (batches 9 and 10).

Reasoning tokens are counted in the output tokens, and billed as output (BRIEF §8). Even
"minimal" reasoning is most of the output: about 77% in batch 9's roles calls.
"""

import json
import math
from collections.abc import Sequence
from typing import Any

from countertalk.llm.base import CallDetails, Message

# Per million tokens (Meta's price list, 1 Nov).
USD_PER_M_INPUT = 1.25
USD_PER_M_CACHED_INPUT = 0.15
USD_PER_M_OUTPUT = 4.25
# An estimate before sending, on the high side: Devanagari takes more tokens per character than
# English and JSON (about 4.6 characters a token in batch 9's live test).
CHARS_PER_TOKEN = 2
# Of a call's output tokens, the percentage spent on reasoning (batch 9: 3,108 of 4,012). Whole
# numbers keep the estimate's arithmetic exact.
REASONING_PERCENT = 77


def cost(input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
    paid_input = (input_tokens - cached_tokens) * USD_PER_M_INPUT
    cached = cached_tokens * USD_PER_M_CACHED_INPUT
    return (paid_input + cached + output_tokens * USD_PER_M_OUTPUT) / 1e6


def call_cost(call: CallDetails) -> float:
    return cost(call.input_tokens or 0, call.cached_input_tokens or 0, call.output_tokens or 0)


def request_chars(messages: Sequence[Message], schema: dict[str, Any]) -> int:
    return sum(len(m.content) for m in messages) + len(json.dumps(schema))


def input_tokens(messages: Sequence[Message], schema: dict[str, Any]) -> int:
    """An estimate of a request's input tokens, on the high side."""
    return math.ceil(request_chars(messages, schema) / CHARS_PER_TOKEN)


def output_tokens(answer_tokens: int) -> int:
    """An estimate of a call's output tokens, its reasoning counted, from its answer's."""
    return math.ceil(answer_tokens * 100 / (100 - REASONING_PERCENT))
