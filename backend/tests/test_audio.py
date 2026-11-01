"""audio.py: join, check, one decode, and chunks for speech-to-text (batch 5).

Audio is made by ffmpeg inside each test: tones, with silences where a test needs pauses. A
browser stream is Opus/WebM written to a pipe (so it stores no duration) and cut into byte
ranges. No audio file is committed (§3.5).
"""

import json
import logging
import subprocess
import tempfile
import wave
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from countertalk import audio, limits, storage
from countertalk.errors import PermanentError, RetryableError
from countertalk.models import new_id

RATE = limits.PREPARED_SAMPLE_RATE
# 2 s of tone, then 1 s of silence, again and again: a pause every 3 s.
# Commas are escaped: lavfi would read them as the filter's own separators.
TALK_WITH_PAUSES = r"0.5*sin(2*PI*440*t)*gt(mod(t\,3)\,1)"
TALK_WITHOUT_PAUSES = "0.5*sin(2*PI*440*t)"
SILENCE = "0"
FAKE_NAME = "Leaky Shopkeeper Name"


def make(expr: str, seconds: float, codec: str, fmt: str, *tags: str, pipe: bool = False) -> bytes:
    """Encoded audio from an expression of t. Written to a file unless `pipe`, as a browser's
    MediaRecorder does: then nothing can go back to store the duration."""
    metadata = [arg for tag in tags for arg in ("-metadata", tag)]
    with tempfile.TemporaryDirectory() as scratch:
        out = "pipe:1" if pipe else str(Path(scratch) / "out")
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi"]
            + ["-i", f"aevalsrc={expr}:s=48000:d={seconds}", *metadata]
            + ["-c:a", codec, "-f", fmt, out],
            capture_output=True,
            check=True,
            timeout=60,
        )
        return result.stdout if pipe else Path(out).read_bytes()


def browser_stream(expr: str, seconds: float) -> bytes:
    return make(expr, seconds, "libopus", "webm", pipe=True)


def wav_file(expr: str, seconds: float) -> bytes:
    return make(expr, seconds, "pcm_s16le", "wav")


def store(data: bytes, piece: int = 64 * 1024) -> tuple[str, list[int]]:
    """A visit whose segments are `data` cut into byte ranges, as the API stores them."""
    visit_id = new_id()
    pieces = [data[start : start + piece] for start in range(0, len(data), piece)]
    for seq, body in enumerate(pieces):
        path = storage.segment_path(visit_id, seq)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    return visit_id, list(range(len(pieces)))


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Each ffprobe and ffmpeg run, by what it was for."""
    seen: list[str] = []
    real_run = audio._run

    def counted(
        args: list[str], timeout_ms: int, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        seen.append("decode" if args[0] == "ffmpeg" else "probe")
        return real_run(args, timeout_ms, cwd)

    monkeypatch.setattr(audio, "_run", counted)
    yield seen


def pcm(path: Path) -> tuple[int, int, int, bytes]:
    with wave.open(str(path), "rb") as audio_file:
        frames = audio_file.getnframes()
        return (
            audio_file.getframerate(),
            audio_file.getnchannels(),
            frames,
            audio_file.readframes(frames),
        )


def probe_seconds(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    return float(out)


# --- preparing ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "ext"),
    [
        (lambda: browser_stream(TALK_WITH_PAUSES, 20), "webm"),
        (lambda: make(TALK_WITH_PAUSES, 20, "aac", "adts"), "aac"),
        (lambda: make(TALK_WITH_PAUSES, 20, "aac", "ipod"), "m4a"),
    ],
    ids=["browser-webm", "phone-aac", "upload-m4a"],
)
def test_a_recording_in_pieces_is_joined_and_prepared(
    data: Callable[[], bytes], ext: str, runs: list[str]
) -> None:
    visit_id, seqs = store(data())

    prepared = audio.prepare(visit_id, ext, seqs)

    rate, channels, frames, _ = pcm(storage.prepared_path(visit_id))
    assert (rate, channels) == (RATE, 1)
    assert prepared.duration_ms == frames * 1000 // RATE
    assert abs(prepared.duration_ms - 20_000) < 150
    assert abs(probe_seconds(storage.playback_path(visit_id)) - 20) < 0.2
    assert [(c.start_ms, c.end_ms) for c in prepared.chunks] == [(0, prepared.duration_ms)]
    assert runs.count("decode") == 1


def test_a_browser_streams_length_comes_from_its_last_packet(runs: list[str]) -> None:
    # Written to a pipe, the WebM stores no duration: the length is read from packets.
    visit_id, seqs = store(browser_stream(TALK_WITH_PAUSES, 20))

    assert audio.probe_duration_ms(audio.join(visit_id, "webm", seqs)) in range(19_900, 20_100)
    assert runs == ["probe", "probe"]  # the container's duration, then the packets


def test_a_gap_in_the_segments_is_permanent_and_runs_nothing(runs: list[str]) -> None:
    visit_id, seqs = store(wav_file(TALK_WITH_PAUSES, 3), piece=1024)

    with pytest.raises(PermanentError) as error:
        audio.prepare(visit_id, "wav", [seq for seq in seqs if seq != 2])

    assert error.value.code == "segments_missing"
    assert runs == []


def test_unreadable_audio_fails_once_permanently_in_plain_words(
    runs: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    visit_id, seqs = store(b"this is not audio at all " * 400)

    with caplog.at_level(logging.WARNING), pytest.raises(PermanentError) as error:
        audio.prepare(visit_id, "webm", seqs)

    assert (error.value.code, error.value.message) == ("unreadable_audio", audio.UNREADABLE)
    assert runs == ["probe"]
    # The rep reads the message; ffprobe's own line, which names a server path, goes to the log.
    assert f"audio unreadable visit={visit_id} tool=ffprobe" in caplog.text


@pytest.mark.parametrize(("limit_ms", "refused"), [(10_000, False), (9_999, True)])
def test_the_length_is_checked_before_any_conversion(
    monkeypatch: pytest.MonkeyPatch, runs: list[str], limit_ms: int, refused: bool
) -> None:
    monkeypatch.setattr(limits, "MAX_VISIT_MS", limit_ms)
    visit_id, seqs = store(wav_file(TALK_WITH_PAUSES, 10))

    if refused:
        with pytest.raises(PermanentError) as error:
            audio.prepare(visit_id, "wav", seqs)
        assert error.value.code == "recording_too_long"
        assert "decode" not in runs
    else:
        assert audio.prepare(visit_id, "wav", seqs).duration_ms == 10_000


@pytest.mark.parametrize(("over_by", "refused"), [(0, False), (1, True)])
def test_the_source_size_is_checked_while_joining(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runs: list[str], over_by: int, refused: bool
) -> None:
    data = wav_file(TALK_WITH_PAUSES, 3)
    monkeypatch.setattr(limits, "MAX_SOURCE_BYTES", len(data) - over_by)
    visit_id, seqs = store(data, piece=4096)

    if refused:
        with pytest.raises(PermanentError) as error:
            audio.prepare(visit_id, "wav", seqs)
        assert error.value.code == "recording_too_large"
        assert runs == []
        assert not storage.source_path(visit_id, "wav").exists()
        assert list((tmp_path / "tmp").glob("*")) == []
    else:
        assert audio.join(visit_id, "wav", seqs).read_bytes() == data


@pytest.mark.parametrize(
    ("amplitude", "speech"),
    [(0, False), (0.0025, False), (0.01, True)],  # silence, about -52 dB, about -40 dB
    ids=["silence", "under-the-threshold", "over-the-threshold"],
)
def test_audio_without_speech_is_refused_and_leaves_nothing(amplitude: float, speech: bool) -> None:
    visit_id, seqs = store(wav_file(f"{amplitude}*sin(2*PI*440*t)", 5))

    if speech:
        audio.prepare(visit_id, "wav", seqs)
        return
    with pytest.raises(PermanentError) as error:
        audio.prepare(visit_id, "wav", seqs)

    assert error.value.code == "no_speech"
    assert not storage.prepared_path(visit_id).exists()
    assert not storage.playback_path(visit_id).exists()


def test_no_tag_is_carried_into_the_prepared_files() -> None:
    # playback.m4a goes to browsers; the recorder's tags stay behind (§1.9).
    visit_id, seqs = store(make(TALK_WITH_PAUSES, 3, "aac", "ipod", f"title={FAKE_NAME}"))

    audio.prepare(visit_id, "m4a", seqs)

    for path in (storage.playback_path(visit_id), storage.prepared_path(visit_id)):
        tags = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout
        assert "Leaky" not in tags


STDERR_ENDING_IN_TAGS = """\
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'source.m4a':
  Metadata:
    title           : Leaky Shopkeeper Name
[aist#0:0/aac @ 0x1] Error while decoding stream #0:0: Invalid data found when processing input
  Stream #0:0[0x1](und): Audio: aac (LC), 48000 Hz, mono, fltp, 69 kb/s
    Metadata:
      artist          : Leaky Shopkeeper Name
"""


def test_an_error_quotes_only_unindented_lines() -> None:
    tail = audio.stderr_tail(STDERR_ENDING_IN_TAGS)

    assert "Leaky" not in tail
    assert "Invalid data found when processing input" in tail


def test_a_tag_that_looks_like_a_measurement_is_ignored() -> None:
    # ffmpeg prints tags among its own lines; only the filters' own lines are read.
    silent = make(SILENCE, 3, "aac", "ipod", "title=max_volume: 0.0 dB")
    visit_id, seqs = store(silent)

    with pytest.raises(PermanentError) as error:
        audio.prepare(visit_id, "m4a", seqs)

    assert error.value.code == "no_speech"


def test_metadata_never_reaches_an_error_message_or_the_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # ffmpeg prints the input's tags, indented, and then fails to open its output; errors quote
    # only unindented lines (§1.9, §2.7).
    visit_id, seqs = store(make(TALK_WITH_PAUSES, 3, "aac", "ipod", f"title={FAKE_NAME}"))
    audio.join(visit_id, "m4a", seqs)  # joined now: prepare reuses it
    monkeypatch.setattr(storage, "new_temp", lambda: tmp_path / "no-such-folder" / "out.part")
    printed: list[str] = []
    real_run = subprocess.run

    def keep_stderr(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        try:
            return real_run(*args, **kwargs)  # type: ignore[call-overload]
        except subprocess.CalledProcessError as failed:
            printed.append(failed.stderr)
            raise

    monkeypatch.setattr(subprocess, "run", keep_stderr)

    with caplog.at_level(logging.WARNING), pytest.raises(PermanentError) as error:
        audio.prepare(visit_id, "m4a", seqs)

    assert FAKE_NAME in "".join(printed)  # ffmpeg did print the tag
    assert error.value.code == "unreadable_audio"
    assert "Leaky" not in error.value.message + caplog.text
    assert "Shopkeeper" not in error.value.message + caplog.text


# --- a retry reuses finished work (§9.4) --------------------------------------------------------


def test_a_retry_reuses_everything_and_runs_nothing(runs: list[str]) -> None:
    visit_id, seqs = store(wav_file(TALK_WITH_PAUSES, 6))
    first = audio.prepare(visit_id, "wav", seqs)
    runs.clear()

    assert audio.prepare(visit_id, "wav", seqs) == first
    assert runs == []


def lose_a_chunk(visit_id: str) -> None:
    storage.chunk_path(visit_id, 0).unlink()


def truncate_prepared(visit_id: str) -> None:
    path = storage.prepared_path(visit_id)
    path.write_bytes(path.read_bytes()[:-100])


def lose_playback(visit_id: str) -> None:
    storage.playback_path(visit_id).unlink()


def break_the_manifest(visit_id: str) -> None:
    storage.chunks_manifest_path(visit_id).write_text("{not json", encoding="utf-8")


def drop_a_field_from_the_manifest(visit_id: str) -> None:
    path = storage.chunks_manifest_path(visit_id)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    del manifest["duration_ms"]
    path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize(
    "damage",
    [
        lose_a_chunk,
        truncate_prepared,
        lose_playback,
        break_the_manifest,
        drop_a_field_from_the_manifest,
    ],
)
def test_a_retry_redoes_what_doesnt_check_out_from_the_source(
    runs: list[str], damage: Callable[[str], None]
) -> None:
    visit_id, seqs = store(wav_file(TALK_WITH_PAUSES, 6))
    first = audio.prepare(visit_id, "wav", seqs)
    source_written = storage.source_path(visit_id, "wav").stat().st_mtime_ns
    damage(visit_id)
    runs.clear()

    assert audio.prepare(visit_id, "wav", seqs) == first
    assert runs.count("decode") == 1
    assert storage.source_path(visit_id, "wav").stat().st_mtime_ns == source_written


# --- chunks lose nothing (§9.6) ------------------------------------------------------------------


def chunk_pcm(visit_id: str, chunks: list[audio.Chunk]) -> bytes:
    return b"".join(pcm(storage.chunk_path(visit_id, c.index))[3] for c in chunks)


def test_chunks_cut_at_pauses_and_lose_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "STT_CHUNK_MS", 10_000)
    monkeypatch.setattr(limits, "PAUSE_SEARCH_MS", 2_000)
    visit_id, seqs = store(wav_file(TALK_WITH_PAUSES, 32))

    prepared = audio.prepare(visit_id, "wav", seqs)

    chunks = prepared.chunks
    _, _, frames, whole = pcm(storage.prepared_path(visit_id))
    assert len(chunks) == 4
    assert chunks[0].start_ms == 0
    assert all(a.end_ms == b.start_ms for a, b in zip(chunks, chunks[1:], strict=False))
    assert chunks[-1].end_ms == prepared.duration_ms
    assert sum(c.frames for c in chunks) == frames
    assert chunk_pcm(visit_id, chunks) == whole
    assert all(c.end_ms - c.start_ms <= limits.STT_CHUNK_MS for c in chunks)
    # Each cut is in a pause: the pattern is silent for the first second of every 3.
    assert all(0 < c.end_ms % 3_000 < 1_000 for c in chunks[:-1])
    manifest = json.loads(storage.chunks_manifest_path(visit_id).read_text(encoding="utf-8"))
    assert [c["start_ms"] for c in manifest["chunks"]] == [c.start_ms for c in chunks]


# Brown-ish noise under the talk: its peaks pass -35 and -30 dB, but not the looser levels.
NOISY_TALK = TALK_WITH_PAUSES + r"+0.1*(random(0)-0.5)"


def test_noisy_audio_is_still_cut_in_its_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "STT_CHUNK_MS", 10_000)
    monkeypatch.setattr(limits, "PAUSE_SEARCH_MS", 2_000)
    visit_id, seqs = store(wav_file(NOISY_TALK, 32))

    prepared = audio.prepare(visit_id, "wav", seqs)

    cuts = prepared.chunks[:-1]
    assert len(cuts) == 3
    assert all(0 < c.end_ms % 3_000 < 1_000 for c in cuts)  # in a pause
    assert all(c.cut in ("longest pause at -25 dB", "longest pause at -20 dB") for c in cuts)


def test_audio_without_a_pause_is_still_cut_at_the_limit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(limits, "STT_CHUNK_MS", 10_000)
    visit_id, seqs = store(wav_file(TALK_WITHOUT_PAUSES, 25))

    with caplog.at_level(logging.WARNING):
        prepared = audio.prepare(visit_id, "wav", seqs)

    assert [(c.start_ms, c.end_ms) for c in prepared.chunks] == [
        (0, 10_000),
        (10_000, 20_000),
        (20_000, 25_000),
    ]
    assert chunk_pcm(visit_id, prepared.chunks) == pcm(storage.prepared_path(visit_id))[3]
    assert f"visit={visit_id}" in caplog.text


L35, L25, L20 = -35, -25, -20


@pytest.mark.parametrize(
    ("frames", "ladder", "cuts"),
    [
        (240_000, [], [(0, 240_000, "end")]),
        (240_024, [], [(0, 240_000, "limit"), (240_000, 240_024, "end")]),
        # The longest pause in the last 3 s before the limit, not the last one.
        (
            300_000,
            [(L35, [(7_500, 8_500), (9_200, 9_600)])],
            [(0, 192_000, "longest pause at -35 dB"), (192_000, 300_000, "end")],
        ),
        # On a tie, the later one.
        (
            300_000,
            [(L35, [(7_200, 7_800), (9_000, 9_600)])],
            [(0, 223_200, "longest pause at -35 dB"), (223_200, 300_000, "end")],
        ),
        # The strictest level that finds a pause there, even if a looser one finds a longer.
        (
            300_000,
            [(L35, [(7_400, 7_800)]), (L20, [(8_000, 9_500)])],
            [(0, 182_400, "longest pause at -35 dB"), (182_400, 300_000, "end")],
        ),
        # A stricter level with nothing there falls back to a looser one.
        (
            300_000,
            [(L35, []), (L25, [(8_000, 8_800)])],
            [(0, 201_600, "longest pause at -25 dB"), (201_600, 300_000, "end")],
        ),
        # The only pause there is a short one.
        (
            300_000,
            [(L35, [(9_000, 9_400)])],
            [(0, 220_800, "longest pause at -35 dB"), (220_800, 300_000, "end")],
        ),
        # Nothing in the last 3 s: the last pause before them.
        (
            300_000,
            [(L35, [(1_000, 2_000), (3_000, 3_600)])],
            [(0, 79_200, "last pause at -35 dB"), (79_200, 300_000, "end")],
        ),
        # A pause that runs past the limit is cut at the limit, inside the pause.
        (
            300_000,
            [(L35, [(9_998, 10_200)])],
            [(0, 240_000, "longest pause at -35 dB"), (240_000, 300_000, "end")],
        ),
        # No pause after a cut: the next one is at the limit.
        (
            480_000,
            [(L35, [(9_000, 9_600)])],
            [
                (0, 223_200, "longest pause at -35 dB"),
                (223_200, 463_200, "limit"),
                (463_200, 480_000, "end"),
            ],
        ),
    ],
    ids=[
        "exactly-the-limit",
        "1-ms-over",
        "longest-not-last",
        "tie-takes-the-later",
        "strictest-level-first",
        "looser-level-if-needed",
        "only-a-short-pause",
        "last-pause-before-the-stretch",
        "pause-straddling-the-limit",
        "pause-then-none",
    ],
)
def test_cuts_are_planned_at_the_best_pause_before_the_limit(
    monkeypatch: pytest.MonkeyPatch,
    frames: int,
    ladder: list[tuple[int, list[tuple[int, int]]]],
    cuts: list[tuple[int, int, str]],
) -> None:
    monkeypatch.setattr(limits, "STT_CHUNK_MS", 10_000)
    monkeypatch.setattr(limits, "PAUSE_SEARCH_MS", 3_000)

    planned = audio.plan_cuts(frames, ladder, RATE)

    assert [(cut.start, cut.end, cut.how) for cut in planned] == cuts
    assert all(cut.end - cut.start <= 240_000 for cut in planned)


@pytest.mark.parametrize("limit", ["MAX_STT_FILE_MS", "MAX_STT_FILE_BYTES"])
def test_a_chunk_over_the_speech_to_text_limits_is_refused(
    monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    # Checked here, before anything is sent: Meta answers 400 or 413 (BRIEF §8).
    monkeypatch.setattr(limits, "STT_CHUNK_MS", 10_000)
    monkeypatch.setattr(limits, limit, 1_000)
    visit_id, seqs = store(wav_file(TALK_WITHOUT_PAUSES, 5))

    with pytest.raises(PermanentError) as error:
        audio.prepare(visit_id, "wav", seqs)

    assert error.value.code == "chunk_over_limit"


def test_the_real_limits_fit_inside_metas() -> None:
    chunk_bytes = 44 + limits.STT_CHUNK_MS * RATE // 1000 * 2  # 16-bit mono WAV
    assert limits.STT_CHUNK_MS < limits.MAX_STT_FILE_MS
    assert chunk_bytes < limits.MAX_STT_FILE_BYTES


# --- found by review --------------------------------------------------------------------------


def test_a_container_that_understates_its_length_is_refused_after_decoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(limits, "MAX_VISIT_MS", 5_000)
    monkeypatch.setattr(audio, "probe_duration_ms", lambda source: 1_000)  # the header says 1 s
    visit_id, seqs = store(wav_file(TALK_WITH_PAUSES, 10))

    with pytest.raises(PermanentError) as error:
        audio.prepare(visit_id, "wav", seqs)

    assert error.value.code == "recording_too_long"
    assert not storage.prepared_path(visit_id).exists()


def test_packets_with_no_duration_still_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # MediaRecorder writes blocks with no duration: ffprobe prints N/A for them.
    def probe(source: Path, entries: list[str]) -> str:
        if "json" in entries:
            return '{"streams": [{"codec_type": "audio"}], "format": {}}'
        return "0.000000,N/A\n0.020000,N/A\n12.340000,N/A\n"

    monkeypatch.setattr(audio, "_probe", probe)

    assert audio.probe_duration_ms(tmp_path / "source.webm") == 12_340


@pytest.mark.parametrize(
    ("failure", "error", "code"),
    [
        (
            subprocess.CalledProcessError(-9, "ffprobe", stderr=""),
            RetryableError,
            "audio_interrupted",
        ),
        (
            subprocess.CalledProcessError(1, "ffprobe", stderr="out.part: No space left on device"),
            RetryableError,
            "disk_full",
        ),
        (FileNotFoundError("ffprobe"), RetryableError, "ffmpeg_missing"),
        (
            subprocess.CalledProcessError(1, "ffprobe", stderr="Invalid data found"),
            PermanentError,
            "unreadable_audio",
        ),
    ],
    ids=["killed", "disk-full", "not-installed", "unreadable"],
)
def test_a_failed_run_is_retried_only_if_retrying_can_fix_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: BaseException,
    error: type[Exception],
    code: str,
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(error) as raised:
        audio.probe_duration_ms(tmp_path / "source.wav")

    assert raised.value.code == code  # type: ignore[attr-defined]


def test_a_redo_with_fewer_chunks_leaves_no_old_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "STT_CHUNK_MS", 10_000)
    visit_id, seqs = store(wav_file(TALK_WITH_PAUSES, 25))
    assert len(audio.prepare(visit_id, "wav", seqs).chunks) == 3
    monkeypatch.setattr(limits, "STT_CHUNK_MS", 60_000)
    truncate_prepared(visit_id)

    assert len(audio.prepare(visit_id, "wav", seqs).chunks) == 1

    assert sorted(p.name for p in storage.chunk_path(visit_id, 0).parent.iterdir()) == [
        "chunk_000.wav"
    ]


def test_a_stream_whose_clock_jumps_is_still_cut_in_its_pauses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pauses are found by timestamp, cuts made by sample: a 2 s jump in the clock at 10 s (a
    # stalled recorder) mustn't move the cuts off the pauses.
    monkeypatch.setattr(limits, "STT_CHUNK_MS", 10_000)
    monkeypatch.setattr(limits, "PAUSE_SEARCH_MS", 2_000)
    jumping = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi"]
        + ["-i", f"aevalsrc={TALK_WITH_PAUSES}:s=48000:d=32"]
        + ["-af", "asetpts='if(gte(T,10),PTS+2/TB,PTS)'"]
        + ["-c:a", "libopus", "-f", "webm", "pipe:1"],
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout
    visit_id, seqs = store(jumping)

    cuts = audio.prepare(visit_id, "webm", seqs).chunks[:-1]

    assert cuts
    assert all(0 < c.end_ms % 3_000 < 1_000 for c in cuts)
