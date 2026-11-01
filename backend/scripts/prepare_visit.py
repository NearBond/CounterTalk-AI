"""Prepares a stored visit's audio for speech-to-text: a dev tool (batch 5).

    uv run python scripts/prepare_visit.py <visit_id>

Batch 6's transcribe job calls the same audio.prepare(). This prints the duration, each chunk's
offsets and size, and the files' sizes: never the recording's metadata or any words.
"""

import sys

from sqlalchemy import select

from countertalk import audio, storage
from countertalk.db import session_scope
from countertalk.errors import DomainError, describe
from countertalk.models import Segment, Visit, parse_id


def main(argv: list[str]) -> int:
    if len(argv) != 1 or parse_id(argv[0]) is None:
        print("usage: prepare_visit.py <visit_id>", file=sys.stderr)
        return 1
    visit_id = parse_id(argv[0])
    assert visit_id is not None
    # Read in one short transaction, then prepare with none open (§2.4).
    with session_scope(read_only=True) as session:
        visit = session.get(Visit, visit_id)
        if visit is None or visit.completed_at is None or visit.source_ext is None:
            print(f"visit {visit_id} isn't a completed upload", file=sys.stderr)
            return 1
        ext = visit.source_ext
        seqs = list(
            session.scalars(
                select(Segment.seq).where(Segment.visit_id == visit_id).order_by(Segment.seq)
            )
        )
    try:
        prepared = audio.prepare(visit_id, ext, seqs)
    except DomainError as error:
        print(f"failed: {describe(error)}", file=sys.stderr)
        return 1
    print(f"visit {visit_id}: duration {prepared.duration_ms / 1000:.1f} s")
    for chunk in prepared.chunks:
        print(
            f"chunk {chunk.index}: {chunk.start_ms / 1000:.1f} s to {chunk.end_ms / 1000:.1f} s"
            f" ({(chunk.end_ms - chunk.start_ms) / 1000:.1f} s, {chunk.bytes:,} bytes, {chunk.cut})"
        )
    for name, path in [
        ("source", storage.source_path(visit_id, ext)),
        ("prepared.wav", storage.prepared_path(visit_id)),
        ("playback.m4a", storage.playback_path(visit_id)),
    ]:
        print(f"{name}: {path.stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
