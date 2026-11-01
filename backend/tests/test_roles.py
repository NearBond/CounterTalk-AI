"""roles.py: who each speaker label is, asked of Muse Spark once per transcript (batch 9).

The LLM is a fake (§3.2) that checks no transaction is open while it's asked (§2.4).
"""

import logging
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from sqlalchemy import select

from countertalk import catalog, limits, llm_calls, roles, visits
from countertalk.db import session_scope
from countertalk.errors import Conflict, NotFound, PermanentError, RetryableError
from countertalk.llm.base import CallDetails, Message, Outcome
from countertalk.models import LlmCall, SpeakerRole, Transcript, Visit, VisitClient
from countertalk.playbooks import OrgPack, load_org_pack, playbook_text
from tests.conftest import ORG_PACK_FIXTURES, give_transcript, lock_waiters, open_transactions
from tests.test_visits import Org_, make_org, new_visit
from tests.test_worker import wait_for

Line = tuple[str, int, str]  # label, start_ms, text


def tiny() -> OrgPack:
    return load_org_pack("tiny-demo", 1, ORG_PACK_FIXTURES)


def tiny_org() -> Org_:
    """An org on the tests' tiny-demo pack."""
    org = make_org()
    with session_scope() as session:
        catalog.import_org_pack(session, org.org_id, tiny())
    return org


@pytest.fixture
def org() -> Org_:
    return tiny_org()


def transcribed(org: Org_, lines: Sequence[Line]) -> str:
    """A visit with a transcript of these turns, as transcription saves one. Its ID."""
    return give_transcript(new_visit(org, client=VisitClient.UPLOAD), lines)


VISIT: list[Line] = [
    ("c0:A", 1_000, "Namaste Asha ji, aaj kya order hai?"),
    ("c0:B", 4_000, "Kurkure ke do dabbe chahiye."),
    ("c0:C", 65_000, "Bhaiya ek Thums Up dena."),
    ("c0:B", 70_000, "Haan, yeh lo."),
]


def speakers(*placed: tuple[str, str, float]) -> dict[str, Any]:
    return {"speakers": [{"label": a, "role": r, "confidence": c} for a, r, c in placed]}


ALL_PLACED = speakers(("c0:A", "rep", 0.9), ("c0:B", "shopkeeper", 0.8), ("c0:C", "other", 0.7))


def call(error: str | None = None) -> CallDetails:
    return CallDetails(
        model="fake-spark-1",
        prompt_hash="ab" * 32,
        latency_ms=1_200,
        input_tokens=900,
        cached_input_tokens=0,
        output_tokens=60,
        reasoning_tokens=20,
        request_id="req-1",
        error=error,
    )


class FakeLlm:
    """Answers request n with `answers[n]`: an answer, or a whole Outcome. Every request checks
    that no transaction is open (§2.4)."""

    provider = "fake"
    model = "fake-spark-1"

    def __init__(
        self,
        *answers: dict[str, Any] | Outcome,
        during: Callable[[int], None] | None = None,
    ) -> None:
        self.answers = answers
        self.during = during
        self.asked: list[tuple[list[Message], str, dict[str, Any]]] = []

    def complete(
        self,
        messages: Sequence[Message],
        name: str,
        schema: dict[str, Any],
        *,
        claim_lost: threading.Event | None = None,
    ) -> Outcome:
        assert open_transactions() == 0, "a transaction was open during an LLM call"
        self.asked.append((list(messages), name, schema))
        if self.during is not None:
            self.during(len(self.asked) - 1)
        answer = self.answers[len(self.asked) - 1]
        return answer if isinstance(answer, Outcome) else Outcome(calls=(call(),), answer=answer)


def saved(transcript_id: str) -> dict[str, tuple[str, float, str, int]]:
    with session_scope(read_only=True) as session:
        rows = session.scalars(
            select(SpeakerRole).where(SpeakerRole.transcript_id == transcript_id)
        )
        return {
            r.speaker_label: (r.role, r.confidence, r.playbook_id, r.playbook_version) for r in rows
        }


def logged_calls(visit_id: str) -> list[tuple[str, str, bool, str | None, int | None]]:
    with session_scope(read_only=True) as session:
        rows = session.scalars(
            select(LlmCall).where(LlmCall.visit_id == visit_id).order_by(LlmCall.seq)
        )
        return [(r.purpose, r.provider, r.ok, r.error, r.input_tokens) for r in rows]


def visit_of(transcript_id: str) -> str:
    with session_scope(read_only=True) as session:
        return session.get_one(Transcript, transcript_id).visit_id


def test_every_label_gets_a_role_saved_with_the_playbook(org: Org_) -> None:
    transcript_id = transcribed(org, VISIT)
    llm = FakeLlm(ALL_PLACED)

    assigned = roles.assign_roles(transcript_id, llm, tiny())

    expected = {"c0:A": ("rep", 0.9), "c0:B": ("shopkeeper", 0.8), "c0:C": ("other", 0.7)}
    assert assigned.roles == expected
    assert saved(transcript_id) == {
        label: (*role, "fmcg-kirana-visit", 1) for label, role in expected.items()
    }
    # The paid call, logged with what it cost (§9.3).
    assert logged_calls(visit_of(transcript_id)) == [("roles", "fake", True, None, 900)]
    assert assigned.calls == (call(),)


def test_the_request_names_every_label_and_the_playbooks_roles(org: Org_) -> None:
    transcript_id = transcribed(org, VISIT)
    llm = FakeLlm(ALL_PLACED)

    roles.assign_roles(transcript_id, llm, tiny())

    [(messages, name, schema)] = llm.asked
    assert name == "speaker_roles"
    assert [m.role for m in messages] == ["system", "user"]
    # The playbook's roles prompt is the system message's end: the same for every visit, so it
    # can be cached.
    assert messages[0].content == roles.INSTRUCTIONS + "\n\n" + playbook_text(
        tiny(), "roles_prompt"
    )
    assert messages[1].content == (
        "Labels: c0:A, c0:B, c0:C\n"
        "\n"
        "[T0][c0:A][00:01] Namaste Asha ji, aaj kya order hai?\n"
        "[T1][c0:B][00:04] Kurkure ke do dabbe chahiye.\n"
        "[T2][c0:C][01:05] Bhaiya ek Thums Up dena.\n"
        "[T3][c0:B][01:10] Haan, yeh lo."
    )
    speaker = schema["properties"]["speakers"]["items"]["properties"]
    assert speaker["label"]["enum"] == ["c0:A", "c0:B", "c0:C"]
    assert speaker["role"]["enum"] == ["rep", "shopkeeper", "other"]


def test_the_instructions_say_two_labels_can_be_one_person() -> None:
    # Within a chunk (rp01 gave the rep both c0:A and c0:B) and across chunks (BRIEF §12).
    assert "in different chunks can be the same person" in roles.INSTRUCTIONS
    assert "Two labels in one chunk can also be the same person" in roles.INSTRUCTIONS


def test_a_long_transcript_is_sampled_with_every_label_in_it(org: Org_) -> None:
    first, sampled = limits.ROLES_FIRST_TURNS, limits.ROLES_SAMPLED_TURNS
    lines: list[Line] = [("c0:A" if i % 2 else "c0:B", i * 1_000, f"turn {i}") for i in range(200)]
    # Neither is among the first turns or the sample.
    lines[152] = ("c1:D", 152_000, "a label heard once, late")
    lines[153] = ("c1:D", 153_000, "and again, longer than the first time")
    transcript_id = transcribed(org, lines)
    llm = FakeLlm(speakers(("c0:A", "rep", 0.9)))

    roles.assign_roles(transcript_id, llm, tiny())

    sent = [
        line.split("]")[0].removeprefix("[T") for line in llm.asked[0][0][1].content.split("\n")[2:]
    ]
    ids = [int(turn) for turn in sent]
    assert ids[:first] == list(range(first))
    # Evenly from the rest, then the longest turn of any label not yet seen.
    rest = list(range(first, 200))
    assert ids[first:] == sorted({rest[i * len(rest) // sampled] for i in range(sampled)} | {153})
    assert len(ids) == first + sampled + 1


def test_in_a_visit_of_several_chunks_every_label_gets_a_role(org: Org_) -> None:
    lines: list[Line] = [
        ("c0:A", 0, "Namaste."),
        ("c0:?", 2_000, "Haan."),
        ("c0:B", 4_000, "Order likho."),
        ("c1:A", 600_000, "Aur kuch?"),
        ("c1:C", 602_000, "Bas itna."),
    ]
    transcript_id = transcribed(org, lines)
    answer = speakers(
        ("c0:A", "rep", 0.9), ("c0:B", "shopkeeper", 0.8), ("c1:C", "shopkeeper", 0.6)
    )
    llm = FakeLlm(answer)

    roles.assign_roles(transcript_id, llm, tiny())

    # A label the reply leaves out is other, with confidence 0 (§9.6).
    assert {label: role[:2] for label, role in saved(transcript_id).items()} == {
        "c0:A": ("rep", 0.9),
        "c0:?": ("other", 0.0),
        "c0:B": ("shopkeeper", 0.8),
        "c1:A": ("other", 0.0),
        "c1:C": ("shopkeeper", 0.6),
    }


@pytest.mark.parametrize(
    "odd",
    [
        {"label": "c9:Z", "role": "rep", "confidence": 0.9},  # a label not in the transcript
        {"label": "c0:C", "role": "boss", "confidence": 0.9},  # a role not in the playbook
        {"label": "c0:C", "role": "rep", "confidence": 1.5},  # a confidence out of 0..1
        {"label": "c0:C", "role": "rep", "confidence": True},  # not a number
    ],
)
def test_an_entry_that_doesnt_fit_is_ignored(
    org: Org_, odd: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    transcript_id = transcribed(org, VISIT)
    answer = speakers(("c0:A", "rep", 0.9), ("c0:B", "shopkeeper", 0.8))
    answer["speakers"].append(odd)

    with caplog.at_level(logging.INFO, logger="countertalk.roles"):
        roles.assign_roles(transcript_id, FakeLlm(answer), tiny())

    # Counted, so a model answering in another shape doesn't go unnoticed (§9.6).
    assert [r.getMessage() for r in caplog.records if r.message.startswith("roles assigned")] == [
        f"roles assigned visit={visit_of(transcript_id)} transcript={transcript_id}"
        " roles=c0:A=rep,c0:B=shopkeeper,c0:C=other unplaced=1 ignored=1"
    ]

    assert {label: role[:2] for label, role in saved(transcript_id).items()} == {
        "c0:A": ("rep", 0.9),
        "c0:B": ("shopkeeper", 0.8),
        "c0:C": ("other", 0.0),
    }


def test_a_label_placed_twice_keeps_its_first_role(org: Org_) -> None:
    transcript_id = transcribed(org, VISIT)
    answer = speakers(
        ("c0:A", "rep", 0.9), ("c0:A", "shopkeeper", 0.95), ("c0:B", "shopkeeper", 0.8)
    )

    roles.assign_roles(transcript_id, FakeLlm(answer), tiny())

    assert saved(transcript_id)["c0:A"][:2] == ("rep", 0.9)


def test_a_reply_that_places_nobody_is_asked_once_more(org: Org_) -> None:
    transcript_id = transcribed(org, VISIT)
    nobody = speakers(("c9:Z", "rep", 0.9))
    llm = FakeLlm(nobody, ALL_PLACED)

    roles.assign_roles(transcript_id, llm, tiny())

    [(first, _, schema), (again, _, same_schema)] = llm.asked
    assert again == [
        *first,
        Message("assistant", '{"speakers": [{"label": "c9:Z", "role": "rep", "confidence": 0.9}]}'),
        Message("user", roles.REASK),
    ]
    assert same_schema == schema
    assert saved(transcript_id)["c0:A"][:2] == ("rep", 0.9)
    assert len(logged_calls(visit_of(transcript_id))) == 2


def test_a_reply_that_places_nobody_twice_fails_clearly(org: Org_) -> None:
    transcript_id = transcribed(org, VISIT)
    llm = FakeLlm({"speakers": []}, {"speakers": []})

    with pytest.raises(PermanentError) as error:
        roles.assign_roles(transcript_id, llm, tiny())

    assert (error.value.code, error.value.message) == (
        "roles_none_placed",
        "Muse Spark didn't say who any speaker was",
    )
    assert saved(transcript_id) == {}
    assert len(logged_calls(visit_of(transcript_id))) == 2  # both paid for, both logged


REJECTED_DETAILS = {"http_status": 400, "request_id": "req-9", "error_type": "invalid_request"}


@pytest.mark.parametrize(
    ("error", "details"),
    [
        (
            RetryableError("llm_unavailable", "Muse Spark is busy"),
            "http_status=None error_type=None",
        ),
        (
            PermanentError("llm_rejected", "Muse Spark couldn't accept this", REJECTED_DETAILS),
            "http_status=400 error_type=invalid_request",
        ),
    ],
)
def test_a_failed_call_is_logged_then_raised(
    org: Org_,
    error: PermanentError | RetryableError,
    details: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    transcript_id = transcribed(org, VISIT)
    failed = Outcome(calls=(call("llm_answer_invalid"), call(error.code)), error=error)

    with caplog.at_level(logging.WARNING), pytest.raises(type(error)) as raised:
        roles.assign_roles(transcript_id, FakeLlm(failed), tiny())

    assert raised.value is error
    # With the client's details, for Meta's support if it repeats (§2.10).
    request_id = (error.details or {}).get("request_id")
    assert [r.getMessage() for r in caplog.records if r.name == "countertalk.llm_calls"] == [
        f"llm call failed visit={visit_of(transcript_id)} purpose=roles code={error.code}"
        f" {details} request_id={request_id}"
    ]
    assert logged_calls(visit_of(transcript_id)) == [
        ("roles", "fake", False, "llm_answer_invalid", 900),
        ("roles", "fake", False, error.code, 900),
    ]
    assert saved(transcript_id) == {}


def test_roles_already_saved_are_never_asked_for_again(org: Org_) -> None:
    transcript_id = transcribed(org, VISIT)
    roles.assign_roles(transcript_id, FakeLlm(ALL_PLACED), tiny())
    again = FakeLlm()

    assigned = roles.assign_roles(transcript_id, again, tiny())

    assert again.asked == []
    assert (assigned.roles["c0:A"], assigned.calls) == (("rep", 0.9), ())
    assert len(logged_calls(visit_of(transcript_id))) == 1


def test_roles_saved_by_another_run_during_the_call_are_kept(org: Org_) -> None:
    # The re-check is inside the write (§9.4): the first run's roles stand.
    transcript_id = transcribed(org, VISIT)

    def another_run_finishes(request: int) -> None:
        roles.assign_roles(transcript_id, FakeLlm(ALL_PLACED), tiny())

    late = speakers(("c0:A", "shopkeeper", 0.5), ("c0:B", "rep", 0.5), ("c0:C", "rep", 0.5))

    assigned = roles.assign_roles(transcript_id, FakeLlm(late, during=another_run_finishes), tiny())

    assert assigned.roles == {
        "c0:A": ("rep", 0.9),
        "c0:B": ("shopkeeper", 0.8),
        "c0:C": ("other", 0.7),
    }
    assert {label: role[:2] for label, role in saved(transcript_id).items()} == assigned.roles
    assert len(logged_calls(visit_of(transcript_id))) == 2  # both runs paid, both logged


def test_a_failed_log_write_doesnt_lose_the_answer(
    org: Org_, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    transcript_id = transcribed(org, VISIT)

    def broken_write(*args: object, **kwargs: object) -> None:
        raise OSError("the database went away")

    monkeypatch.setattr(llm_calls, "session_scope", broken_write)

    with caplog.at_level(logging.WARNING):
        assigned = roles.assign_roles(transcript_id, FakeLlm(ALL_PLACED), tiny())

    assert assigned.roles["c0:A"] == ("rep", 0.9)
    assert saved(transcript_id)["c0:A"][:2] == ("rep", 0.9)
    visit_id = visit_of(transcript_id)
    assert [r.getMessage() for r in caplog.records if r.name == "countertalk.llm_calls"] == [
        f"llm calls not logged visit={visit_id} purpose=roles calls=1"
    ]


def test_prompts_and_replies_are_never_logged(org: Org_, caplog: pytest.LogCaptureFixture) -> None:
    transcript_id = transcribed(org, VISIT)
    nobody = speakers(("c9:Z-from-the-answer", "rep", 0.9))

    with caplog.at_level(logging.DEBUG):
        roles.assign_roles(transcript_id, FakeLlm(nobody, ALL_PLACED), tiny())

    assert "roles assigned" in caplog.text  # it did log
    for words in ("Asha", "Kurkure", "Thums", "Z-from-the-answer", "same person"):
        assert words not in caplog.text


def test_an_org_on_another_pack_is_refused(org: Org_) -> None:
    # Each org uses only its own pack (BRIEF §12): its playbook's roles.
    transcript_id = transcribed(org, VISIT)
    llm = FakeLlm(ALL_PLACED)

    with pytest.raises(Conflict) as error:
        roles.assign_roles(transcript_id, llm, load_org_pack("pepsico-india-demo", 1))

    assert (error.value.code, llm.asked) == ("wrong_org_pack", [])


def test_an_unknown_transcript_is_not_found() -> None:
    with pytest.raises(NotFound) as error:
        roles.assign_roles("00000000-0000-4000-8000-000000000000", FakeLlm(), tiny())

    assert error.value.code == "transcript_not_found"


def test_a_jobs_claim_lost_is_passed_to_the_client(org: Org_) -> None:
    transcript_id = transcribed(org, VISIT)
    claim_lost = threading.Event()
    seen: list[threading.Event | None] = []

    class Watching(FakeLlm):
        def complete(
            self, *args: Any, claim_lost: threading.Event | None = None, **kw: Any
        ) -> Outcome:
            seen.append(claim_lost)
            return super().complete(*args, claim_lost=claim_lost, **kw)

    roles.assign_roles(transcript_id, Watching(ALL_PLACED), tiny(), claim_lost=claim_lost)

    assert seen == [claim_lost]


FIRST = {"c0:A": ("rep", 0.9), "c0:B": ("shopkeeper", 0.8), "c0:C": ("other", 0.7)}


def test_two_runs_writing_at_once_keep_the_first(
    org: Org_, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The visit's lock makes the re-check and the write one step (§2.3): the run that waited
    # finds the other's roles, rather than failing on the duplicate.
    transcript_id = transcribed(org, VISIT)
    visit_id = visit_of(transcript_id)
    logged, go = threading.Event(), threading.Event()
    record = llm_calls.record

    def record_then_wait(*args: Any) -> None:
        # The second run has its answer and has logged its call: it's about to write.
        record(*args)
        logged.set()
        assert go.wait(timeout=10)

    monkeypatch.setattr(llm_calls, "record", record_then_wait)
    late = speakers(("c0:A", "shopkeeper", 0.5), ("c0:B", "rep", 0.5), ("c0:C", "rep", 0.5))
    with ThreadPoolExecutor(max_workers=1) as pool:
        second_run = pool.submit(roles.assign_roles, transcript_id, FakeLlm(late), tiny())
        try:
            assert logged.wait(timeout=10)
            with session_scope() as session:  # the first run's write, holding the visit's lock
                visits.lock_visit(session, org.org_id, visit_id)
                session.add_all(
                    SpeakerRole(
                        transcript_id=transcript_id,
                        speaker_label=label,
                        role=role,
                        confidence=confidence,
                        playbook_id="fmcg-kirana-visit",
                        playbook_version=1,
                    )
                    for label, (role, confidence) in FIRST.items()
                )
                session.flush()
                go.set()
                wait_for(lambda: lock_waiters() == 1, "the second run never waited for the lock")
        finally:
            go.set()
        assigned = second_run.result(timeout=10)

    assert assigned.roles == FIRST
    assert {label: role[:2] for label, role in saved(transcript_id).items()} == FIRST


def test_roles_come_back_in_the_order_their_labels_are_first_heard(org: Org_) -> None:
    lines: list[Line] = [("c2:A", 0, "Namaste."), ("c10:A", 1_000, "Haan."), ("c2:B", 2_000, "Ji.")]
    transcript_id = transcribed(org, lines)
    answer = speakers(("c2:B", "other", 0.5), ("c10:A", "shopkeeper", 0.8), ("c2:A", "rep", 0.9))

    first = roles.assign_roles(transcript_id, FakeLlm(answer), tiny())
    again = roles.assign_roles(transcript_id, FakeLlm(), tiny())

    assert list(first.roles) == list(again.roles) == ["c2:A", "c10:A", "c2:B"]


def test_a_deleted_visits_calls_stay_counted(org: Org_) -> None:
    transcript_id = transcribed(org, VISIT)
    visit_id = visit_of(transcript_id)
    roles.assign_roles(transcript_id, FakeLlm(ALL_PLACED), tiny())

    with session_scope() as session:
        session.delete(session.get_one(Visit, visit_id))

    with session_scope(read_only=True) as session:
        rows = session.execute(select(LlmCall.visit_id, LlmCall.input_tokens)).all()
        assert [tuple(row) for row in rows] == [(None, 900)]
        assert session.scalars(select(SpeakerRole)).all() == []
