"""Every filesystem path under DATA_DIR (CODING_GUIDELINES §2.6).

Paths are built only from validated values: a visit ID that is a plain UUID, a segment number in
range, an extension from a fixed list. Nothing from a request is ever joined into a path as
it came. Files are written to a temporary file first and moved into place with os.replace, so
a crash never leaves half a file where a whole one should be.
"""

import logging
import os
import stat
import time
import uuid
from pathlib import Path

from countertalk.config import get_settings
from countertalk.errors import Invalid
from countertalk.limits import MAX_SEGMENTS_PER_VISIT
from countertalk.models import parse_id

log = logging.getLogger(__name__)

# The extensions an uploaded stream may have (complete(ext)); batch 5 names the joined file so.
ALLOWED_EXTS = ("webm", "m4a", "mp4", "aac", "ogg", "wav")
# Chunks of a visit: an hour is 7 of 9.5 minutes, so 1,000 is far beyond any real one.
MAX_CHUNKS = 1_000


def data_dir() -> Path:
    return get_settings().DATA_DIR


def _staging_dir() -> Path:
    # Inside DATA_DIR, so it's on the same filesystem and os.replace is atomic.
    return data_dir() / "tmp"


def visit_dir(visit_id: str) -> Path:
    # Only the canonical form: a path is never built from any other spelling of an ID.
    if parse_id(visit_id) != visit_id:
        raise Invalid("invalid_id", "That isn't a visit ID")
    return data_dir() / "visits" / visit_id


def check_seq(seq: int) -> int:
    if isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq < MAX_SEGMENTS_PER_VISIT:
        raise Invalid(
            "invalid_seq", f"A segment number runs from 0 to {MAX_SEGMENTS_PER_VISIT - 1}"
        )
    return seq


def check_ext(ext: str) -> str:
    if ext not in ALLOWED_EXTS:
        raise Invalid(
            "unsupported_extension", "The recording's type isn't one of " + ", ".join(ALLOWED_EXTS)
        )
    return ext


def segment_path(visit_id: str, seq: int) -> Path:
    # Opaque bytes: a segment after the first may not be playable on its own (BRIEF §2).
    return visit_dir(visit_id) / "segments" / f"{check_seq(seq):06d}.bin"


def source_path(visit_id: str, ext: str) -> Path:
    """The visit's segments joined into one file, with the extension complete() gave."""
    return visit_dir(visit_id) / f"source.{check_ext(ext)}"


def prepared_path(visit_id: str) -> Path:
    """16-bit mono WAV at 24 kHz, for speech-to-text."""
    return visit_dir(visit_id) / "prepared.wav"


def playback_path(visit_id: str) -> Path:
    return visit_dir(visit_id) / "playback.m4a"


def chunk_path(visit_id: str, index: int) -> Path:
    if isinstance(index, bool) or not 0 <= index < MAX_CHUNKS:
        raise Invalid("invalid_chunk", f"A chunk number runs from 0 to {MAX_CHUNKS - 1}")
    return visit_dir(visit_id) / "chunks" / f"chunk_{index:03d}.wav"


def chunks_manifest_path(visit_id: str) -> Path:
    """Written last: while it's there and checks out, the preparation is finished."""
    return visit_dir(visit_id) / "chunks.json"


def new_temp() -> Path:
    """A fresh path in the staging folder, for a file moved into place once it's whole. One a
    crash leaves behind is swept, like any staged file."""
    staging = _staging_dir()
    staging.mkdir(parents=True, exist_ok=True)
    return staging / f"{uuid.uuid4().hex}.part"


def stage(body: bytes | bytearray) -> Path:
    """Writes the bytes to a new temporary file, flushed to disk, and returns its path."""
    path = new_temp()
    try:
        with path.open("xb") as file:
            file.write(body)
            file.flush()
            os.fsync(file.fileno())
    except BaseException:
        # A full disk or an I/O error must not leave half a file behind (§9.10).
        path.unlink(missing_ok=True)
        raise
    return path


def sync(path: Path) -> None:
    """Flushes a file another writer made (ffmpeg, wave) to disk, before it moves into place."""
    with path.open("rb+") as file:
        os.fsync(file.fileno())


def move_into_place(staged: Path, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staged, path)
    if os.name == "posix":
        # The rename is only on disk once its folder is: without this, a crash after the commit
        # could lose a file the database already records. (Windows can't open a folder.)
        folder = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)


def discard(staged: Path) -> None:
    """Removes a staged file, if it's still there. Never anything outside the staging folder."""
    if staged.parent.resolve() != _staging_dir().resolve():
        raise ValueError("discard() only removes staged files")
    staged.unlink(missing_ok=True)


def sweep_staged(older_than_ms: int) -> int:
    """Deletes staged files older than this, left by a crash between staging and cleanup.

    Only regular `.part` files in the staging folder, and only old ones, so an upload in
    progress is never touched. Safe while another worker sweeps: a file it took first is skipped.
    """
    cutoff = time.time() - older_than_ms / 1000
    try:
        entries = list(_staging_dir().iterdir())
    except FileNotFoundError:
        return 0
    removed = 0
    for path in entries:
        if path.suffix != ".part":
            continue
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mtime >= cutoff:
                continue
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as error:
            # One stuck file (held open on Windows, say) doesn't stop the sweep.
            log.warning("staged file not swept error=%s", type(error).__name__)
            continue
        removed += 1
    return removed


def relative_to_data_dir(path: Path) -> str:
    """The form stored in the database, so DATA_DIR can move. Raises ValueError outside it."""
    return path.resolve().relative_to(data_dir().resolve()).as_posix()
