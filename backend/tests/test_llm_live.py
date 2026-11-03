"""Muse Spark for real: does strict mode take the extraction schema (batch 9)? Paid, so only
with -m live.

    uv run pytest -m live -s tests/test_llm_live.py

The schema is the demo org pack's, built by build_extraction_schema, so it has the type lists
of llm/schema.py's `nullable_schema` (["string", "null"], an enum with null). If Meta refuses
them, its error body is printed and `nullable_schema` switches to anyOf. One short call: about
$0.01 at most. The prompt is made up, with no transcript in it.
"""

import json

import httpx2
import pytest

from countertalk.llm.base import Message
from countertalk.llm.client import MetaLlm
from countertalk.playbooks import build_extraction_schema, load_org_pack
from tests.conftest import print_utf8

PROMPT = (
    "This shop visit had no insights, no tasks and no order. Reply with empty insights, tasks"
    ' and order_items lists, summary_en "Nothing to report.", and next_visit_date null.'
)


class Recording(httpx2.BaseTransport):
    """The real transport, keeping each reply's status and the start of its body."""

    def __init__(self) -> None:
        self.inner = httpx2.HTTPTransport()
        self.replies: list[tuple[int, str]] = []

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        response = self.inner.handle_request(request)
        response.read()
        self.replies.append((response.status_code, response.text[:800]))
        return response


@pytest.mark.live
def test_strict_mode_takes_the_extraction_schema(live_key: str) -> None:
    pack = load_org_pack("pepsico-india-demo", 1)
    schema = build_extraction_schema(pack.playbook, pack)
    transport = Recording()

    outcome = MetaLlm(live_key, transport=transport).complete(
        [Message("system", "Reply with JSON only."), Message("user", PROMPT)], "extraction", schema
    )

    report = {
        "replies": transport.replies if outcome.error else [s for s, _ in transport.replies],
        "error": outcome.error and f"{outcome.error.code}: {outcome.error.details}",
        "calls": [vars(call) for call in outcome.calls],
        "answer": outcome.answer,
    }
    print_utf8(json.dumps(report, indent=1, ensure_ascii=False))
    assert outcome.error is None
