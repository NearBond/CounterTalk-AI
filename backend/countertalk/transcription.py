"""Transcription: the `transcribe` job's handler (BRIEF §2; CODING_GUIDELINES §2.4, §2.5, §9.3).

1. Lock the visit and set it transcribing, in a short transaction.
2. Prepare its audio (audio.prepare), with no transaction open.
3. Transcribe each chunk that has no saved reply. Each reply is saved the moment it arrives, so
   a retry never pays for a chunk twice (Meta bills a reply that's lost). Before each call, stop
   if the claim was lost, so two workers don't both pay.
   Each paid call adds its billed seconds and time to the visit, in a short transaction.
4. In a second transaction, save the transcript, its turns and the duration, then set the visit
   transcribed.

Safe to run twice: only a visit that's uploaded or transcribing is worked on, so one with a
transcript is left alone. By status, not by transcript_id: batch 13's reprocess sends a visit
back to uploaded with its old transcript still current, and expects a new one. So a visit
that's uploaded with a transcript is being transcribed again: the replies that made that
transcript are removed under its lock as it starts, and every chunk is paid for again. The
transcript being replaced is noted beside the replies, so a re-run that fails part-way and is
reprocessed again keeps what it paid for; so does one that failed part-way with no transcript
yet: a retry never pays twice (R8).
"""

import json
import logging
import threading
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from countertalk import audio, catalog, errors, jobs, storage, visits
from countertalk.db import session_scope
from countertalk.errors import DomainError, NotFound, PermanentError
from countertalk.models import JobKind, Org, Segment, Transcript, Turn, Visit, VisitStatus
from countertalk.playbooks import DEFAULT_LANGUAGES, ORG_PACKS_DIR, PLAYBOOKS_DIR, load_org_pack
from countertalk.stt.base import Reply, SpeechToText

log = logging.getLogger(__name__)

# Recognition is biased towards these (ISO 639-1) for an org without a pack; an org's pack can
# name its own (batch T3). The manual test checks the hint helps.
LANGUAGES = DEFAULT_LANGUAGES
SAVED_REPLY_VERSION = 1


@dataclass(frozen=True)
class _Work:
    ext: str
    seqs: list[int]
    org_id: str


@dataclass(frozen=True)
class SavedReply:
    """One chunk's reply as saved on disk (stt/chunk_000.json, …), with what it was for."""

    chunk: dict[str, int]
    provider: str
    model: str
    languages: list[str]
    session_id: str | None
    wall_ms: int
    reply: dict[str, Any]
    # What speech-to-text was told to listen for (batch 8). A reply saved before keywords were
    # sent has none, which is just what it was sent with.
    keywords: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TranscriptView:
    transcript: Transcript
    turns: list[Turn]


def current_transcript(session: Session, org_id: str, visit_id: str) -> TranscriptView:
    """The visit's current transcript and its turns, in order."""
    visit = visits.get_visit(session, org_id, visit_id)
    transcript = session.get(Transcript, visit.transcript_id) if visit.transcript_id else None
    if transcript is None:
        raise NotFound("transcript_not_found", "This visit hasn't been transcribed yet")
    turns = session.scalars(
        select(Turn).where(Turn.transcript_id == transcript.id).order_by(Turn.turn_id)
    )
    return TranscriptView(transcript, list(turns))


def transcribe(
    visit_id: str,
    stt: SpeechToText,
    *,
    claim_lost: threading.Event | None = None,
    languages: Sequence[str] | None = None,
    packs: Path = ORG_PACKS_DIR,
    playbooks: Path = PLAYBOOKS_DIR,
) -> None:
    """Transcribes a visit that's waiting for it, biased towards `languages`, or else its org
    pack's. Raises PermanentError or RetryableError."""
    work = _start(visit_id)
    if work is None:
        return
    if languages is None:
        hint = visit_languages(visit_id, work.org_id, packs, playbooks)
    else:
        hint = list(languages)
    prepared = audio.prepare(visit_id, work.ext, work.seqs)
    keywords = visit_keywords(visit_id, work.org_id)
    replies = [
        _reply_for(visit_id, chunk, stt, hint, keywords, claim_lost) for chunk in prepared.chunks
    ]
    _finish(visit_id, stt, hint, prepared, replies)


def org_languages(
    org_id: str, packs: Path = ORG_PACKS_DIR, playbooks: Path = PLAYBOOKS_DIR
) -> tuple[str, ...]:
    """The languages speech-to-text listens for in this org's visits: its pack's, or the
    default for an org with no pack yet (whose visits have no catalog keywords either)."""
    with session_scope(read_only=True) as session:
        org = session.get_one(Org, org_id)
        pack_id, version = org.org_pack_id, org.org_pack_version
    if pack_id is None or version is None:
        return DEFAULT_LANGUAGES
    return load_org_pack(pack_id, version, packs, playbooks).languages


def _start(visit_id: str) -> _Work | None:
    with session_scope() as session:
        visit = visits.lock_for_work(session, visit_id)
        if visit is None:
            return None  # a job's visit can't go without the job; nothing to do either way
        if visit.status == VisitStatus.UPLOADED:
            if visit.transcript_id is not None:  # reprocessed from transcribe (batch 13)
                _start_again(visit_id, visit.transcript_id)
            visits.set_status(visit, VisitStatus.TRANSCRIBING)
        elif visit.status != VisitStatus.TRANSCRIBING:  # a retry finds it transcribing
            log.info("transcription not needed visit=%s status=%s", visit_id, visit.status)
            return None
        if visit.source_ext is None:
            raise PermanentError("upload_not_finished", "The recording's upload never finished")
        seqs = session.scalars(
            select(Segment.seq).where(Segment.visit_id == visit_id).order_by(Segment.seq)
        )
        return _Work(visit.source_ext, list(seqs), visit.org_id)


def _start_again(visit_id: str, replaced: str) -> None:
    """The replies that made the transcript being replaced go, once: a note of it is left
    beside them, so replies a later run of this same redo paid for are kept (R8). Harmless if
    the transaction then rolls back: the note stops a second removal."""
    note = storage.replacing_path(visit_id)
    try:
        noted = note.read_text(encoding="utf-8").strip()
    except OSError:
        noted = None
    if noted == replaced:
        log.info("transcribing again, replies kept visit=%s", visit_id)
        return
    removed = storage.remove_stt_replies(visit_id)
    storage.write_file(note, replaced.encode())
    log.info("transcribing again visit=%s replies_removed=%d", visit_id, removed)


def _chunk_key(chunk: audio.Chunk) -> dict[str, int]:
    return {"index": chunk.index, "start_ms": chunk.start_ms, "end_ms": chunk.end_ms}


def _first_sent(visit_id: str, key: str, missing: list[str] | None) -> list[str] | None:
    """What chunk 0's saved reply was sent with under `key` (`missing` if a reply written before
    it had none), or None: nothing paid for yet, or not what was written."""
    try:
        first = json.loads(storage.stt_reply_path(visit_id, 0).read_text(encoding="utf-8"))
        if first["version"] == SAVED_REPLY_VERSION:
            kept = first.get(key, missing)
            if isinstance(kept, list) and all(isinstance(word, str) for word in kept):
                return kept
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def visit_keywords(visit_id: str, org_id: str) -> list[str]:
    """The keywords this visit's chunks are sent with. Once a reply is paid for, its keywords
    stay the visit's: a catalog edit before a retry mustn't make saved replies stop matching
    (§9.4). Before that, the org's own, read with no lock held."""
    kept = _first_sent(visit_id, "keywords", [])
    if kept is not None:
        return kept
    with session_scope(read_only=True) as session:
        return catalog.keywords_for(session, org_id)


def visit_languages(
    visit_id: str, org_id: str, packs: Path = ORG_PACKS_DIR, playbooks: Path = PLAYBOOKS_DIR
) -> list[str]:
    """The languages this visit's chunks are sent with, kept like its keywords: once a reply
    is paid for, a pack change before a retry mustn't make saved replies stop matching (§9.4).
    Before that, the org pack's."""
    kept = _first_sent(visit_id, "languages", None)
    return list(org_languages(org_id, packs, playbooks)) if kept is None else kept


def saved_reply(
    visit_id: str, chunk: audio.Chunk, stt: SpeechToText, languages: list[str], keywords: list[str]
) -> SavedReply | None:
    """The reply an earlier try saved for this very audio and request, if any (§9.4)."""
    try:
        path = storage.stt_reply_path(visit_id, chunk.index)
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.pop("version") != SAVED_REPLY_VERSION:
            return None
        saved = SavedReply(**data)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None  # missing, or not what was written
    # The same audio, sent the same way: a reply made without keywords isn't one made with them.
    same = (saved.chunk, saved.provider, saved.model, saved.languages, saved.keywords) == (
        _chunk_key(chunk),
        stt.provider,
        stt.model,
        languages,
        keywords,
    )
    return saved if same and isinstance(saved.reply, dict) else None


def _reply_for(
    visit_id: str,
    chunk: audio.Chunk,
    stt: SpeechToText,
    languages: list[str],
    keywords: list[str],
    claim_lost: threading.Event | None,
) -> SavedReply:
    saved = saved_reply(visit_id, chunk, stt, languages, keywords)
    if saved is not None:
        return saved
    if claim_lost is not None and claim_lost.is_set():
        raise errors.claim_lost()
    wav = storage.chunk_path(visit_id, chunk.index).read_bytes()
    log.info(
        "stt call visit=%s chunk=%d audio_ms=%d",
        visit_id,
        chunk.index,
        chunk.end_ms - chunk.start_ms,
    )
    try:
        reply = stt.transcribe(wav, languages, keywords)
    except DomainError as error:
        # With the client's session and status, and the provider's own words for a refusal, for
        # its support if a failure repeats.
        found = error.details or {}
        log.warning(
            "stt call failed visit=%s chunk=%d code=%s session=%s http_status=%s error_type=%s"
            " error_message=%r",
            visit_id,
            chunk.index,
            error.code,
            found.get("session_id"),
            found.get("http_status"),
            found.get("error_type"),
            found.get("error_message"),
        )
        raise
    # Another run of this visit (one whose claim was lost) may have saved its reply first: the
    # transcript is made from the first, so keep that one.
    saved = saved_reply(visit_id, chunk, stt, languages, keywords)
    if saved is None:
        saved = SavedReply(
            chunk=_chunk_key(chunk),
            provider=stt.provider,
            model=stt.model,
            languages=languages,
            session_id=reply.session_id,
            wall_ms=reply.wall_ms,
            reply=reply.body,
            keywords=keywords,
        )
        record = {"version": SAVED_REPLY_VERSION, **asdict(saved)}
        path = storage.stt_reply_path(visit_id, chunk.index)
        storage.write_file(path, json.dumps(record, ensure_ascii=False, indent=1).encode())
    _count_paid_call(visit_id, chunk, stt, reply)
    return saved


def _count_paid_call(visit_id: str, chunk: audio.Chunk, stt: SpeechToText, reply: Reply) -> None:
    """Adds the call's billed seconds and time to the visit, every paid call, reused or not
    (§9.3). Meta bills each request in whole seconds; a reply that doesn't say counts in full."""
    length = chunk.end_ms - chunk.start_ms
    billed_ms = stt.parse(reply.body, length).billed_ms
    billed_s = (length if billed_ms is None else billed_ms) // 1000
    log.info(
        "stt reply visit=%s chunk=%d session=%s billed_s=%d wall_ms=%d",
        visit_id,
        chunk.index,
        reply.session_id,
        billed_s,
        reply.wall_ms,
    )
    with session_scope() as session:
        visits.add_stt_cost(session, visit_id, billed_s, reply.wall_ms)


def add_transcript(
    session: Session,
    visit: Visit,
    provider: str,
    model: str,
    languages: str,
    turns: Sequence[dict[str, Any]],
) -> str:
    """A new transcript of these turns (turn_id, speaker_label, start_ms, end_ms, text), made
    the visit's current one. The transcribe job's; scripts and tests that need a transcript
    without speech-to-text use it too (R1). Its ID."""
    transcript = Transcript(
        visit_id=visit.id,
        provider=provider,
        model=model,
        language_code=languages or None,
        raw_dir=storage.relative_to_data_dir(storage.stt_dir(visit.id)),
    )
    session.add(transcript)
    session.flush()
    session.execute(insert(Turn), [{"transcript_id": transcript.id, **t} for t in turns])
    visit.transcript_id = transcript.id
    return transcript.id


def _finish(
    visit_id: str,
    stt: SpeechToText,
    languages: list[str],
    prepared: audio.Prepared,
    replies: list[SavedReply],
) -> None:
    turns: list[dict[str, Any]] = []
    for chunk, saved in zip(prepared.chunks, replies, strict=True):
        for turn in stt.parse(saved.reply, chunk.end_ms - chunk.start_ms).turns:
            turns.append(
                {
                    "turn_id": len(turns),
                    "speaker_label": f"c{chunk.index}:{turn.speaker}",
                    "start_ms": chunk.start_ms + turn.start_ms,
                    "end_ms": chunk.start_ms + turn.end_ms,
                    "text": turn.text,
                }
            )
    if not turns:
        raise PermanentError("no_words_heard", "Speech-to-text heard no words in the recording")
    with session_scope() as session:
        visit = visits.lock_for_work(session, visit_id)
        if visit is None or visit.status != VisitStatus.TRANSCRIBING:
            # Another run of it finished first, or it was failed meanwhile.
            status = None if visit is None else visit.status
            log.info("transcript not saved visit=%s status=%s", visit_id, status)
            return
        add_transcript(session, visit, stt.provider, stt.model, ",".join(languages), turns)
        visit.duration_ms = prepared.duration_ms
        visits.set_status(visit, VisitStatus.TRANSCRIBED)
        # In the same transaction: a transcribed visit is never left without its next step.
        jobs.enqueue(session, JobKind.EXTRACT, visit_id=visit_id)
    log.info("transcribed visit=%s chunks=%d turns=%d", visit_id, len(prepared.chunks), len(turns))
