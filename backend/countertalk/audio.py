"""Audio preparation (BRIEF §2, "After the visit"): the only module that runs ffmpeg (§2.7).

prepare() turns a visit's uploaded segments into what speech-to-text takes:
1. Join first: segments 0..N, byte for byte, into source.<ext>. A browser segment after the
   first has no WebM header, so segments are never probed one by one.
2. Read next: ffprobe. Unreadable audio is a PermanentError (§9.5).
3. The length is checked against MAX_VISIT_MS before anything is converted: the container's
   duration, or for a browser's WebM, which has none, the end of its last audio packet.
4. One decode writes prepared.wav (24 kHz, mono, 16-bit), playback.m4a, the loudest level
   (silent audio is "no speech found") and the pauses.
5. prepared.wav is cut into chunks of at most STT_CHUNK_MS, at the last pause before the limit,
   by sample offsets: exact, and no second decode. chunks.json lists them, and is written last.

A retry reuses finished work (§9.4): a whole source.<ext>, and everything after it if
chunks.json and every file it lists check out. Every file is written whole, then moved into
place. Errors quote only the last unindented lines of ffmpeg's stderr: the indented ones are the
recording's metadata tags, which can hold personal data (§1.9).
"""

import json
import logging
import re
import subprocess
import tempfile
import wave
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

# limits as a module, so a test can shorten one for everything here.
from countertalk import limits, storage
from countertalk.errors import Invalid, PermanentError, RetryableError

log = logging.getLogger(__name__)

UNREADABLE = "The recording couldn't be read as audio"
PLAYBACK_BITRATE = "64k"
COPY_BLOCK_BYTES = 1024 * 1024
MANIFEST_VERSION = 2
WAV_HEADER_BYTES = 44  # what the wave module writes for PCM
MAX_VOLUME = re.compile(r"max_volume: (-?[\d.]+|-inf) dB")
SILENCE_START = re.compile(r"lavfi\.silence_start=(-?[\d.]+)")
SILENCE_END = re.compile(r"lavfi\.silence_end=(-?[\d.]+)")


@dataclass(frozen=True)
class Chunk:
    index: int
    start_ms: int
    end_ms: int
    frames: int
    bytes: int
    # How its end was chosen, as plan_cuts says: a pause and its level, the limit, or the end.
    cut: str


@dataclass(frozen=True)
class Prepared:
    # From the decoded audio, not the container's estimate.
    duration_ms: int
    chunks: list[Chunk]


def prepare(visit_id: str, ext: str, seqs: Sequence[int]) -> Prepared:
    """Prepares a visit's audio for speech-to-text, reusing whatever a retry finds finished.

    Files and ffmpeg only, no database: call it with no transaction open (§2.4).
    """
    try:
        return _prepare(visit_id, ext, seqs)
    except PermanentError as error:
        if error.details:  # an unreadable recording: the tool's own lines, for the log only
            log.warning(
                "audio unreadable visit=%s tool=%s stderr=%s",
                visit_id,
                error.details.get("tool"),
                error.details.get("stderr"),
            )
        raise


def _prepare(visit_id: str, ext: str, seqs: Sequence[int]) -> Prepared:
    finished = _finished(visit_id)
    if finished is not None:
        return finished
    # Redoing: until the new chunks.json is written, nothing here counts as finished.
    storage.chunks_manifest_path(visit_id).unlink(missing_ok=True)
    source = join(visit_id, ext, seqs)
    if probe_duration_ms(source) > limits.MAX_VISIT_MS:
        raise _too_long()
    ladder = _decode(visit_id, source)
    prepared = _cut(visit_id, ladder, source.stat().st_size)
    log.info(
        "audio prepared visit=%s duration_ms=%d chunks=%d",
        visit_id,
        prepared.duration_ms,
        len(prepared.chunks),
    )
    return prepared


def _too_long() -> PermanentError:
    minutes = limits.MAX_VISIT_MS // 60_000
    return PermanentError("recording_too_long", f"The recording is over {minutes} minutes long")


def _too_large() -> PermanentError:
    mib = limits.MAX_SOURCE_BYTES // 2**20
    return PermanentError("recording_too_large", f"The recording is over {mib} MiB")


def join(visit_id: str, ext: str, seqs: Sequence[int]) -> Path:
    """Segments 0..N, byte for byte, into source.<ext>; a whole one from a retry is reused."""
    missing = PermanentError("segments_missing", "Some pieces of the recording are missing")
    if not seqs or list(seqs) != list(range(len(seqs))):
        raise missing
    parts = [storage.segment_path(visit_id, seq) for seq in seqs]
    try:
        total = sum(part.stat().st_size for part in parts)
    except FileNotFoundError:
        raise missing from None
    source = storage.source_path(visit_id, ext)
    if source.is_file() and source.stat().st_size == total:
        return source
    if total > limits.MAX_SOURCE_BYTES:
        raise _too_large()
    written = 0
    with storage.writing(source) as out:
        for part in parts:
            with part.open("rb") as piece:
                while block := piece.read(COPY_BLOCK_BYTES):
                    written += len(block)
                    if written > limits.MAX_SOURCE_BYTES:  # a segment grew since
                        raise _too_large()
                    out.write(block)
    return source


def probe_duration_ms(source: Path) -> int:
    """The container's duration, or the end of the last audio packet when it stores none."""
    found = json.loads(
        _probe(source, ["-show_entries", "format=duration:stream=codec_type", "-of", "json"])
        or "{}"
    )
    if not any(stream.get("codec_type") == "audio" for stream in found.get("streams", [])):
        raise PermanentError("unreadable_audio", "The recording has no audio in it")
    duration = found.get("format", {}).get("duration", "N/A")
    if duration != "N/A" and float(duration) > 0:
        return round(float(duration) * 1000)
    # Packets are read without decoding them, so this stays quick for an hour's recording.
    packets = _probe(
        source,
        [
            "-select_streams",
            "a:0",
            "-show_entries",
            "packet=pts_time,duration_time",
            "-of",
            "csv=p=0",
        ],
    )
    ends = [_packet_end(line) for line in packets.splitlines()]
    ends = [end for end in ends if end is not None]
    if not ends:
        raise PermanentError("unreadable_audio", "The recording has no audio in it")
    return round(max(ends) * 1000)


def _packet_end(line: str) -> float | None:
    """Where a packet ends: its timestamp, plus its duration if ffprobe knows it. MediaRecorder
    writes blocks with no duration, which ffprobe prints as N/A."""
    pts, _, duration = line.partition(",")
    try:
        start = float(pts)
    except ValueError:  # N/A: a packet with no timestamp
        return None
    try:
        return start + float(duration)
    except ValueError:
        return start


def _probe(source: Path, entries: list[str]) -> str:
    args = ["ffprobe", "-v", "error", *entries, str(source)]
    return _run(args, limits.FFPROBE_TIMEOUT_MS).stdout


Ladder = list[tuple[int, list[tuple[int, int]]]]


def _decode(visit_id: str, source: Path) -> Ladder:
    """One decode: prepared.wav, playback.m4a, the loudest level, and the pauses at each level.

    Each pause level writes its own file: ffmpeg 7.1 doesn't name filter instances in its log,
    so the levels couldn't be told apart there. ffmpeg runs in a scratch folder, so the file
    names need no escaping.
    """
    levels = limits.SILENCE_THRESHOLDS_DB
    # The levels run one after another on one branch, each clearing its marks for the next:
    # ffmpeg 7.1 aborts on side branches that end in a sink instead of an output.
    detect = ",".join(
        f"silencedetect=noise={db}dB:duration={limits.SILENCE_MIN_MS / 1000},"
        f"ametadata=mode=print:file=pauses{index}.txt,ametadata=mode=delete"
        for index, db in enumerate(levels)
    )
    graph = (
        f"[0:a]aresample={limits.PREPARED_SAMPLE_RATE},"
        # Timestamps from the sample count: a pause found at a time is cut at that sample, even
        # if the recording's own clock jumped.
        "aformat=sample_fmts=s16:channel_layouts=mono,asetpts=N/SR/TB,asplit=3[wav][m4a][check];"
        f"[check]volumedetect,{detect}[sink]"
    )
    wav_part, m4a_part = storage.new_temp(), storage.new_temp()
    try:
        with tempfile.TemporaryDirectory() as scratch:
            result = _run(
                ["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-y"]
                # Never more than a second past the limit, whatever the container claims.
                + ["-t", str(limits.MAX_VISIT_MS / 1000 + 1), "-i", str(source)]
                + ["-filter_complex", graph]
                # Each output carries no tags: playback.m4a goes to browsers (§1.9).
                + ["-map", "[wav]", "-map_metadata", "-1", "-c:a", "pcm_s16le"]
                + ["-f", "wav", str(wav_part)]
                + ["-map", "[m4a]", "-map_metadata", "-1", "-c:a", "aac", "-b:a", PLAYBACK_BITRATE]
                + ["-f", "mp4", "-movflags", "+faststart", str(m4a_part)]
                + ["-map", "[sink]", "-f", "null", "-"],
                limits.FFMPEG_DECODE_TIMEOUT_MS,
                cwd=Path(scratch),
            )
            ladder = [
                (db, _pauses(Path(scratch, f"pauses{index}.txt").read_text(encoding="utf-8")))
                for index, db in enumerate(levels)
            ]
        if _loudest_db(result.stderr) < limits.NO_SPEECH_MAX_DB:
            raise PermanentError("no_speech", "No speech was found in the recording")
        with wave.open(str(wav_part), "rb") as decoded:
            if decoded.getnframes() * 1000 // decoded.getframerate() > limits.MAX_VISIT_MS:
                raise _too_long()  # the container understated its length
        for part, path in (
            (wav_part, storage.prepared_path(visit_id)),
            (m4a_part, storage.playback_path(visit_id)),
        ):
            storage.sync(part)
            storage.move_into_place(part, path)
    finally:
        storage.drop_temp(wav_part)
        storage.drop_temp(m4a_part)
    return ladder


def _loudest_db(stderr: str) -> float:
    # Only volumedetect's own lines: a metadata tag can't pass for a measurement.
    for line in stderr.splitlines():
        if line[:1] == "[" and "volumedetect" in line and (found := MAX_VOLUME.search(line)):
            return float("-inf") if found.group(1) == "-inf" else float(found.group(1))
    return float("-inf")


def _pauses(printed: str) -> list[tuple[int, int]]:
    """Pauses from ametadata's lines: lavfi.silence_start=…, then lavfi.silence_end=…"""
    pauses: list[tuple[int, int]] = []
    start: float | None = None
    for line in printed.splitlines():
        if found := SILENCE_START.fullmatch(line.strip()):
            start = float(found.group(1))
        elif (found := SILENCE_END.fullmatch(line.strip())) and start is not None:
            pauses.append((round(start * 1000), round(float(found.group(1)) * 1000)))
            start = None
    return pauses


@dataclass(frozen=True)
class Cut:
    start: int  # frames
    end: int
    # How the end was chosen: "longest pause at -35 dB", "last pause at -25 dB", "limit", "end".
    how: str


def plan_cuts(total_frames: int, ladder: Ladder, rate: int, visit_id: str = "") -> list[Cut]:
    """Pieces of at most STT_CHUNK_MS each. A cut is at the longest pause in the PAUSE_SEARCH_MS
    before the limit, at the strictest level that finds one there (the later one on a tie);
    else at the last pause before that stretch; with no pause at all, at the limit."""
    per_ms = rate // 1000  # cuts fall on whole milliseconds
    cuts: list[Cut] = []
    start = 0
    while total_frames - start > limits.STT_CHUNK_MS * per_ms:
        start_ms = start // per_ms
        end_ms = start_ms + limits.STT_CHUNK_MS
        cut_ms, how = _choose(
            ladder, start_ms, max(start_ms, end_ms - limits.PAUSE_SEARCH_MS), end_ms
        )
        if cut_ms is None:
            log.warning("no pause to cut at visit=%s at_ms=%d", visit_id, end_ms)
            cut_ms, how = end_ms, "limit"
        else:
            log.info("chunk cut visit=%s at_ms=%d %s", visit_id, cut_ms, how)
        cuts.append(Cut(start, cut_ms * per_ms, how))
        start = cut_ms * per_ms
    cuts.append(Cut(start, total_frames, "end"))
    return cuts


def _choose(ladder: Ladder, start_ms: int, search_ms: int, end_ms: int) -> tuple[int | None, str]:
    # A pause's cut point is its middle, or the limit if it runs past it: always in silence.
    def points(pauses: list[tuple[int, int]]) -> list[tuple[int, int]]:
        return [(e - s, min((s + e) // 2, end_ms)) for s, e in pauses if s < end_ms]

    for db, pauses in ladder:
        found = [(length, point) for length, point in points(pauses) if point > search_ms]
        if found:
            return max(found)[1], f"longest pause at {db} dB"
    for db, pauses in ladder:
        found = [point for _, point in points(pauses) if start_ms < point <= search_ms]
        if found:
            return max(found), f"last pause at {db} dB"
    return None, "limit"


def _cut(visit_id: str, ladder: Ladder, source_bytes: int) -> Prepared:
    """Cuts prepared.wav into chunks and writes chunks.json, last."""
    prepared_path = storage.prepared_path(visit_id)
    with wave.open(str(prepared_path), "rb") as whole:
        params = whole.getparams()
        rate, frame_bytes = params.framerate, params.sampwidth * params.nchannels
        cuts = plan_cuts(params.nframes, ladder, rate, visit_id)
        chunks = [
            Chunk(
                index,
                cut.start * 1000 // rate,
                cut.end * 1000 // rate,
                cut.end - cut.start,
                WAV_HEADER_BYTES + (cut.end - cut.start) * frame_bytes,
                cut.how,
            )
            for index, cut in enumerate(cuts)
        ]
        # Before anything is sent: Meta refuses longer (400) or larger (413) files.
        for chunk in chunks:
            over_ms = chunk.end_ms - chunk.start_ms > limits.MAX_STT_FILE_MS
            if over_ms or chunk.bytes > limits.MAX_STT_FILE_BYTES:
                raise PermanentError(
                    "chunk_over_limit", "A piece of the recording is over speech-to-text's limits"
                )
        for chunk, cut in zip(chunks, cuts, strict=True):
            whole.setpos(cut.start)
            with storage.writing(storage.chunk_path(visit_id, chunk.index)) as file:
                with wave.open(file, "wb") as out:  # closed first: it writes the header last
                    out.setparams(params)
                    out.writeframes(whole.readframes(chunk.frames))
    # Only chunks an earlier, longer cut left behind: each of this cut's is written whole, so a
    # run of the same visit at the same time loses nothing here.
    keep = {storage.chunk_path(visit_id, chunk.index).name for chunk in chunks}
    for old in storage.chunk_path(visit_id, 0).parent.glob("chunk_*.wav"):
        if old.name not in keep:
            old.unlink(missing_ok=True)
    prepared = Prepared(params.nframes * 1000 // rate, chunks)
    manifest = {
        "version": MANIFEST_VERSION,
        "source_bytes": source_bytes,
        "prepared": {"bytes": prepared_path.stat().st_size, "frames": params.nframes},
        "playback_bytes": storage.playback_path(visit_id).stat().st_size,
        "duration_ms": prepared.duration_ms,
        "chunks": [asdict(chunk) for chunk in chunks],
    }
    storage.write_file(
        storage.chunks_manifest_path(visit_id), json.dumps(manifest, indent=2).encode()
    )
    return prepared


def _finished(visit_id: str) -> Prepared | None:
    """The earlier preparation, if chunks.json and every file it lists check out."""
    try:
        manifest = json.loads(storage.chunks_manifest_path(visit_id).read_text(encoding="utf-8"))
        if manifest["version"] != MANIFEST_VERSION:
            return None
        prepared = storage.prepared_path(visit_id)
        with wave.open(str(prepared), "rb") as whole:
            frames = whole.getnframes()
        if (prepared.stat().st_size, frames) != (
            manifest["prepared"]["bytes"],
            manifest["prepared"]["frames"],
        ):
            return None
        if storage.playback_path(visit_id).stat().st_size != manifest["playback_bytes"]:
            return None
        chunks = [Chunk(**chunk) for chunk in manifest["chunks"]]
        if any(storage.chunk_path(visit_id, c.index).stat().st_size != c.bytes for c in chunks):
            return None
        return Prepared(int(manifest["duration_ms"]), chunks)
    except (OSError, ValueError, KeyError, TypeError, EOFError, wave.Error, Invalid):
        return None  # missing, or not what was written: redo it from the source


def _run(
    args: list[str], timeout_ms: int, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """ffmpeg or ffprobe (§2.7): an argument list, check=True and a timeout."""
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=timeout_ms / 1000,
        )
    except FileNotFoundError:
        # A server set up wrong: a retry works once it's fixed, and the recording isn't at fault.
        raise RetryableError("ffmpeg_missing", f"{args[0]} isn't installed on the server") from None
    except subprocess.CalledProcessError as error:
        detail = stderr_tail(error.stderr)
        if error.returncode < 0:  # stopped by a signal, such as running out of memory
            raise RetryableError("audio_interrupted", f"{args[0]} was stopped part-way") from None
        if "No space left on device" in (error.stderr or ""):
            raise RetryableError("disk_full", "The server's disk is full") from None
        # The rep reads the message as the visit's failure reason, so it's fixed words. The tool's
        # own lines name server paths: prepare() logs them, with the visit.
        raise PermanentError(
            "unreadable_audio", UNREADABLE, details={"tool": args[0], "stderr": detail}
        ) from None
    except subprocess.TimeoutExpired:
        raise RetryableError(
            "audio_timeout", f"{args[0]} took over {timeout_ms // 1000} s"
        ) from None


def stderr_tail(stderr: str | None) -> str:
    """The last few unindented lines: indented ones list metadata tags (§1.9)."""
    lines = [line.strip() for line in (stderr or "").splitlines() if line[:1].strip()]
    return " | ".join(lines[-3:])[:300] or "no details"
