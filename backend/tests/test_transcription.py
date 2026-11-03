"""transcription.py: the transcribe handler (batch 6), with a fake speech-to-text client.

Audio is made by ffmpeg in each test, with chunks shortened to 10 s so a visit has several.
"""

import json
import logging
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from countertalk import audio, catalog, limits, storage, transcription, worker
from countertalk.db import session_scope
from countertalk.errors import PermanentError, RetryableError
from countertalk.models import (
    CatalogItem,
    Job,
    JobKind,
    JobState,
    Transcript,
    Turn,
    VisitClient,
    VisitStatus,
)
from countertalk.playbooks import load_org_pack
from countertalk.stt import meta
from countertalk.stt.base import Reply
from tests.conftest import ORG_PACK_FIXTURES, edit_file, open_transactions
from tests.test_audio import TALK_WITH_PAUSES, wav_file
from tests.test_visits import Org_, complete, fail_processing, load, make_org, new_visit, upload


@pytest.fixture
def org() -> Org_:
    return make_org()


@pytest.fixture(autouse=True)
def short_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "STT_CHUNK_MS", 10_000)
    monkeypatch.setattr(limits, "PAUSE_SEARCH_MS", 2_000)


def reply_body(call: int) -> dict[str, Any]:
    return {
        "sessionId": f"fake-session-{call}",
        "transcript": f"call {call} first call {call} second",
        "audioDurationMs": 4_500,
        "turns": [
            {
                "turnId": 1,
                "speaker": "A",
                "startMs": 100,
                "endMs": 1_000,
                "transcript": f"call {call} first",
            },
            {
                "turnId": 2,
                "speaker": "B",
                "startMs": 1_500,
                "endMs": 3_000,
                "transcript": f"call {call} second",
            },
        ],
    }


class FakeStt:
    """Answers call n with `answers[n]` (a reply body, or an error to raise), or a reply of two
    turns. Every call checks that no transaction is open (§2.4)."""

    provider = "fake"
    model = "fake-transcribe-1"
    parse = staticmethod(meta.parse)

    def __init__(
        self,
        answers: dict[int, dict[str, Any] | BaseException] | None = None,
        during: Callable[[int], None] | None = None,
    ) -> None:
        self.answers = answers or {}
        self.during = during
        self.calls: list[bytes] = []
        self.languages: list[tuple[str, ...]] = []
        self.keywords: list[tuple[str, ...]] = []

    def transcribe(self, wav: bytes, languages: Sequence[str], keywords: Sequence[str]) -> Reply:
        assert open_transactions() == 0, "a transaction was open during a speech-to-text call"
        call = len(self.calls)
        self.calls.append(wav)
        self.languages.append(tuple(languages))
        self.keywords.append(tuple(keywords))
        if self.during is not None:
            self.during(call)
        answer = self.answers.get(call, reply_body(call))
        if isinstance(answer, BaseException):
            raise answer
        return Reply(answer, answer.get("sessionId"), wall_ms=1_000 + call)


def uploaded_visit(org: Org_, seconds: float, body: bytes | None = None) -> str:
    visit_id = new_visit(org, client=VisitClient.UPLOAD)
    upload(org, visit_id, 0, body if body is not None else wav_file(TALK_WITH_PAUSES, seconds))
    complete(org, visit_id, last_seq=0, ext="wav")
    return visit_id


def chunks(visit_id: str) -> list[audio.Chunk]:
    return audio.prepare(visit_id, "wav", [0]).chunks  # finished already: reused, nothing runs


def chunk_bytes(visit_id: str, index: int) -> bytes:
    return storage.chunk_path(visit_id, index).read_bytes()


def transcripts(visit_id: str) -> list[Transcript]:
    with session_scope(read_only=True) as session:
        return list(session.scalars(select(Transcript).where(Transcript.visit_id == visit_id)))


def turns_of(transcript_id: str) -> list[tuple[int, str, int, int, str]]:
    with session_scope(read_only=True) as session:
        rows = session.scalars(
            select(Turn).where(Turn.transcript_id == transcript_id).order_by(Turn.turn_id)
        )
        return [(t.turn_id, t.speaker_label, t.start_ms, t.end_ms, t.text) for t in rows]


def saved(visit_id: str, index: int) -> dict[str, Any]:
    return json.loads(storage.stt_reply_path(visit_id, index).read_text(encoding="utf-8"))


def test_a_visit_is_transcribed_into_turns_shifted_by_each_chunks_offset(org: Org_) -> None:
    visit_id = uploaded_visit(org, 15)
    fake = FakeStt()

    transcription.transcribe(visit_id, fake, languages=("hi", "en"))

    first, second = chunks(visit_id)
    visit = load(visit_id)
    [transcript] = transcripts(visit_id)
    assert visit.status == VisitStatus.TRANSCRIBED
    assert visit.transcript_id == transcript.id
    assert visit.duration_ms == second.end_ms
    assert visit.stt_seconds_billed == 2 * 4  # whole seconds, as Meta bills
    assert visit.stt_wall_ms == 1_000 + 1_001
    assert (transcript.provider, transcript.model, transcript.language_code) == (
        "fake",
        "fake-transcribe-1",
        "hi,en",
    )
    assert transcript.raw_dir == f"visits/{visit_id}/stt"
    assert fake.languages == [("hi", "en")] * 2
    offset = second.start_ms
    assert turns_of(transcript.id) == [
        (0, "c0:A", 100, 1_000, "call 0 first"),
        (1, "c0:B", 1_500, 3_000, "call 0 second"),
        (2, "c1:A", offset + 100, offset + 1_000, "call 1 first"),
        (3, "c1:B", offset + 1_500, offset + 3_000, "call 1 second"),
    ]
    reply = saved(visit_id, 1)
    assert (reply["session_id"], reply["wall_ms"], reply["reply"]) == (
        "fake-session-1",
        1_001,
        reply_body(1),
    )
    assert (reply["chunk"]["start_ms"], reply["chunk"]["end_ms"]) == (offset, second.end_ms)
    # Its next step, queued with it (batch 10): a transcribed visit never sits without one.
    with session_scope(read_only=True) as session:
        query = select(Job.state).where(Job.visit_id == visit_id, Job.kind == JobKind.EXTRACT)
        assert session.scalars(query).all() == [JobState.QUEUED]


def test_a_retry_sends_only_the_chunks_with_no_saved_reply(
    org: Org_, caplog: pytest.LogCaptureFixture
) -> None:
    visit_id = uploaded_visit(org, 25)
    found = {
        "session_id": "failed-session",
        "http_status": 429,
        "error_type": "rate_limit",
        "error_message": "Slow down",
    }
    failed = RetryableError("stt_rate_limited", "Speech-to-text is busy", details=found)
    failing = FakeStt({1: failed})
    with caplog.at_level(logging.WARNING), pytest.raises(RetryableError):
        transcription.transcribe(visit_id, failing)
    # The failed call's session and Meta's own words, with its visit and chunk, for the
    # provider's support.
    assert [r.getMessage() for r in caplog.records if r.message.startswith("stt call failed")] == [
        f"stt call failed visit={visit_id} chunk=1 code=stt_rate_limited session=failed-session"
        " http_status=429 error_type=rate_limit error_message='Slow down'"
    ]
    assert (load(visit_id).status, load(visit_id).stt_seconds_billed) == (
        VisitStatus.TRANSCRIBING,
        4,  # chunk 0 was paid for; the failed call isn't billed
    )

    retry = FakeStt()
    transcription.transcribe(visit_id, retry)

    assert len(chunks(visit_id)) == 3
    assert len(failing.calls) == 2
    assert load(visit_id).stt_seconds_billed == 3 * 4
    assert retry.calls == [chunk_bytes(visit_id, 1), chunk_bytes(visit_id, 2)]
    [transcript] = transcripts(visit_id)
    texts = [turn[4] for turn in turns_of(transcript.id)]
    assert texts[:2] == ["call 0 first", "call 0 second"]  # chunk 0, from the first try


def corrupt(change: Callable[[dict[str, Any]], object]) -> Callable[[str], None]:
    def apply(visit_id: str) -> None:
        reply = saved(visit_id, 0)
        change(reply)
        storage.stt_reply_path(visit_id, 0).write_text(json.dumps(reply), encoding="utf-8")

    return apply


def truncate(visit_id: str) -> None:
    path = storage.stt_reply_path(visit_id, 0)
    path.write_bytes(path.read_bytes()[:20])


@pytest.mark.parametrize(
    "spoil",
    [
        truncate,
        corrupt(lambda r: r["chunk"].update(start_ms=r["chunk"]["start_ms"] + 1)),  # cut again
        corrupt(lambda r: r.update(provider="another")),
        corrupt(lambda r: r.update(model="an-older-model")),
        corrupt(lambda r: r.update(reply="not an object")),
    ],
    ids=[
        "truncated",
        "other-audio",
        "other-provider",
        "other-model",
        "bad-reply",
    ],
)
def test_a_saved_reply_that_doesnt_check_out_is_not_reused(
    org: Org_, spoil: Callable[[str], None]
) -> None:
    visit_id = uploaded_visit(org, 15)
    with pytest.raises(RetryableError):
        transcription.transcribe(visit_id, FakeStt({1: RetryableError("stt_timeout", "Slow")}))
    spoil(visit_id)

    retry = FakeStt()
    transcription.transcribe(visit_id, retry)

    assert retry.calls == [chunk_bytes(visit_id, 0), chunk_bytes(visit_id, 1)]


def test_a_reply_saved_with_other_languages_is_sent_again(org: Org_) -> None:
    # The first reply's languages are the visit's (batch T3), so a later one's are checked.
    visit_id = uploaded_visit(org, 25)
    with pytest.raises(RetryableError):
        transcription.transcribe(visit_id, FakeStt({2: RetryableError("stt_timeout", "Slow")}))
    other = saved(visit_id, 1)
    other["languages"] = []
    storage.stt_reply_path(visit_id, 1).write_text(json.dumps(other), encoding="utf-8")

    retry = FakeStt()
    transcription.transcribe(visit_id, retry)

    assert retry.calls == [chunk_bytes(visit_id, 1), chunk_bytes(visit_id, 2)]


def transcribed_already(org: Org_) -> str:
    visit_id = uploaded_visit(org, 5)
    transcription.transcribe(visit_id, FakeStt())
    return visit_id


def failed_before(org: Org_) -> str:
    visit_id = uploaded_visit(org, 5)
    fail_processing(visit_id)
    return visit_id


@pytest.mark.parametrize(
    ("make", "status", "count"),
    [(transcribed_already, VisitStatus.TRANSCRIBED, 1), (failed_before, VisitStatus.FAILED, 0)],
    ids=["has-a-transcript", "failed"],
)
def test_a_visit_not_waiting_for_transcription_is_left_alone(
    org: Org_, make: Callable[[Org_], str], status: str, count: int
) -> None:
    visit_id = make(org)
    again = FakeStt()

    transcription.transcribe(visit_id, again)

    assert again.calls == []
    assert load(visit_id).status == status
    assert len(transcripts(visit_id)) == count


def test_a_run_that_finishes_second_keeps_the_first_runs_transcript(org: Org_) -> None:
    # Two workers on one visit, as after a lost claim: the inner run finishes during the outer
    # run's call.
    visit_id = uploaded_visit(org, 5)
    inner = FakeStt({0: {**reply_body(0), "sessionId": "inner"}})
    outer = FakeStt(during=lambda call: transcription.transcribe(visit_id, inner))

    transcription.transcribe(visit_id, outer)

    [transcript] = transcripts(visit_id)
    assert [turn[4] for turn in turns_of(transcript.id)] == ["call 0 first", "call 0 second"]
    assert load(visit_id).transcript_id == transcript.id
    assert saved(visit_id, 0)["session_id"] == "inner"  # what the transcript was made from
    assert load(visit_id).stt_seconds_billed == 2 * 4  # both runs paid for it


def test_a_lost_claim_stops_before_the_next_paid_call(org: Org_) -> None:
    visit_id = uploaded_visit(org, 15)
    lost = threading.Event()
    fake = FakeStt(during=lambda call: lost.set())

    with pytest.raises(RetryableError) as error:
        transcription.transcribe(visit_id, fake, claim_lost=lost)

    assert error.value.code == "claim_lost"
    assert len(fake.calls) == 1
    assert storage.stt_reply_path(visit_id, 0).is_file()  # the paid reply is kept


NO_WORDS = {"sessionId": "s", "transcript": "", "turns": [], "audioDurationMs": 4_000}


def run_job(fake: FakeStt) -> Job:
    handlers = {JobKind.TRANSCRIBE: worker.transcribe_with(fake)}
    assert worker.Worker("test-host:1:aaaaaa", handlers=handlers).run_once()
    with session_scope(read_only=True) as session:
        return session.scalars(select(Job).where(Job.kind == JobKind.TRANSCRIBE)).one()


@pytest.mark.parametrize(
    ("body", "fake", "reason"),
    [
        (
            None,
            FakeStt({0: PermanentError("stt_auth_failed", "Speech-to-text refused the key")}),
            "Speech-to-text refused the key",
        ),
        (None, FakeStt({0: NO_WORDS}), "Speech-to-text heard no words in the recording"),
        (b"not audio at all", FakeStt(), audio.UNREADABLE),
    ],
    ids=["stt-refused", "no-words", "unreadable"],
)
def test_a_failed_transcription_fails_the_visit_with_a_reason(
    org: Org_, body: bytes | None, fake: FakeStt, reason: str
) -> None:
    visit_id = uploaded_visit(org, 5, body)

    job = run_job(fake)

    visit = load(visit_id)
    assert job.state == JobState.DEAD
    assert visit.status == VisitStatus.FAILED
    assert visit.failure_reason == reason
    assert transcripts(visit_id) == []


def test_the_workers_transcribe_handler_sends_nothing_without_a_key(org: Org_) -> None:
    # MODEL_API_KEY is empty in tests (conftest), as in any process the builder starts.
    uploaded_visit(org, 5)

    assert worker.Worker("test-host:1:bbbbbb").run_once()

    with session_scope(read_only=True) as session:
        job = session.scalars(select(Job).where(Job.kind == JobKind.TRANSCRIBE)).one()
    assert (job.kind, job.state) == (JobKind.TRANSCRIBE, JobState.QUEUED)
    assert job.last_error is not None and "stt_not_configured" in job.last_error


def test_the_visits_orgs_brands_and_products_are_sent_as_keywords(org: Org_) -> None:
    with session_scope() as session:
        catalog.import_org_pack(
            session, org.org_id, load_org_pack("tiny-demo", 1, ORG_PACK_FIXTURES)
        )
    visit_id = uploaded_visit(org, 5)
    fake = FakeStt()

    transcription.transcribe(visit_id, fake, packs=ORG_PACK_FIXTURES)

    assert fake.keywords == [("Crunchy", "Rival", "Crunchy Salted")]
    assert saved(visit_id, 0)["keywords"] == ["Crunchy", "Rival", "Crunchy Salted"]


def test_a_retry_sends_the_keywords_its_first_reply_was_sent_with(org: Org_) -> None:
    # A catalog edit between tries must not make the saved replies stop matching (§9.4).
    with session_scope() as session:
        catalog.import_org_pack(
            session, org.org_id, load_org_pack("tiny-demo", 1, ORG_PACK_FIXTURES)
        )
    visit_id = uploaded_visit(org, 15)
    with pytest.raises(RetryableError):
        transcription.transcribe(
            visit_id, FakeStt({1: RetryableError("stt_timeout", "Slow")}), packs=ORG_PACK_FIXTURES
        )
    with session_scope() as session:
        session.execute(update(CatalogItem).where(CatalogItem.key == "rival").values(active=False))

    retry = FakeStt()
    transcription.transcribe(visit_id, retry, packs=ORG_PACK_FIXTURES)

    assert retry.calls == [chunk_bytes(visit_id, 1)]
    assert retry.keywords == [("Crunchy", "Rival", "Crunchy Salted")]


def test_a_reply_saved_before_keywords_counts_as_sent_without_them(org: Org_) -> None:
    visit_id = uploaded_visit(org, 15)
    with pytest.raises(RetryableError):
        transcription.transcribe(visit_id, FakeStt({1: RetryableError("stt_timeout", "Slow")}))
    older = saved(visit_id, 0)
    del older["keywords"]  # as batch 6 wrote it
    storage.stt_reply_path(visit_id, 0).write_text(json.dumps(older), encoding="utf-8")

    retry = FakeStt()
    transcription.transcribe(visit_id, retry)

    assert retry.calls == [chunk_bytes(visit_id, 1)]


def test_a_reply_saved_with_other_keywords_is_sent_again(org: Org_) -> None:
    # A reply made without keywords isn't one made with them (batch 6's left-over).
    with session_scope() as session:
        catalog.import_org_pack(
            session, org.org_id, load_org_pack("tiny-demo", 1, ORG_PACK_FIXTURES)
        )
    visit_id = uploaded_visit(org, 25)
    with pytest.raises(RetryableError):
        transcription.transcribe(
            visit_id, FakeStt({2: RetryableError("stt_timeout", "Slow")}), packs=ORG_PACK_FIXTURES
        )
    without = saved(visit_id, 1)
    without["keywords"] = []
    storage.stt_reply_path(visit_id, 1).write_text(json.dumps(without), encoding="utf-8")

    retry = FakeStt()
    transcription.transcribe(visit_id, retry, packs=ORG_PACK_FIXTURES)

    assert retry.calls == [chunk_bytes(visit_id, 1), chunk_bytes(visit_id, 2)]
    assert retry.keywords == [("Crunchy", "Rival", "Crunchy Salted")] * 2


# --- the hint from the org's pack (batch T3) --------------------------------------------------


def test_an_org_without_a_pack_is_heard_in_hindi_and_english(org: Org_) -> None:
    visit_id = uploaded_visit(org, 5)
    fake = FakeStt()

    transcription.transcribe(visit_id, fake)

    assert fake.languages == [("hi", "en")]


def test_the_hint_is_the_orgs_pack_s_languages(org: Org_, pack_roots: tuple[Path, Path]) -> None:
    packs, books = pack_roots
    edit_file(
        packs / "tiny-demo/v1/org_pack.yaml", "catalog:\n", "languages: [mr, hi, en]\ncatalog:\n"
    )
    with session_scope() as session:
        catalog.import_org_pack(session, org.org_id, load_org_pack("tiny-demo", 1, packs, books))
    visit_id = uploaded_visit(org, 5)
    fake = FakeStt()

    handlers = {JobKind.TRANSCRIBE: worker.transcribe_with(fake, packs=packs, playbooks=books)}
    assert worker.Worker("test-host:1:aaaaaa", handlers=handlers).run_once()

    [transcript] = transcripts(visit_id)
    assert fake.languages == [("mr", "hi", "en")]
    assert transcript.language_code == "mr,hi,en"
    assert saved(visit_id, 0)["languages"] == ["mr", "hi", "en"]


def test_a_retry_keeps_the_languages_its_first_reply_was_sent_with(
    org: Org_, pack_roots: tuple[Path, Path]
) -> None:
    # A pack change between tries must not make the saved replies stop matching (§9.4).
    packs, books = pack_roots
    with session_scope() as session:
        catalog.import_org_pack(session, org.org_id, load_org_pack("tiny-demo", 1, packs, books))
    visit_id = uploaded_visit(org, 15)
    with pytest.raises(RetryableError):
        transcription.transcribe(
            visit_id,
            FakeStt({1: RetryableError("stt_timeout", "Slow")}),
            packs=packs,
            playbooks=books,
        )
    edit_file(packs / "tiny-demo/v1/org_pack.yaml", "catalog:\n", "languages: [mr, hi]\ncatalog:\n")

    retry = FakeStt()
    transcription.transcribe(visit_id, retry, packs=packs, playbooks=books)

    assert retry.calls == [chunk_bytes(visit_id, 1)]
    assert retry.languages == [("hi", "en")]
