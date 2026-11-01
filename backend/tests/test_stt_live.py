"""Muse Voice Transcribe for real: a 2-second clip (batch 6). Paid, so only with -m live.

    uv run pytest -m live -s tests/test_stt_live.py

It costs about $0.0001. The clip is the start of batch T1's rp01 (generated speech, not a real
visit), so -s may print its words.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from countertalk import limits, transcription
from countertalk.config import BACKEND_DIR, Settings
from countertalk.stt.meta import MetaSpeechToText

SAMPLE = BACKEND_DIR.parent / "data" / "samples" / "rp01.wav"
CLIP_MS = 2_000


@pytest.fixture
def clip(tmp_path: Path) -> bytes:
    """rp01's first words, as speech-to-text takes them: 24 kHz mono 16-bit WAV."""
    if not SAMPLE.is_file():
        pytest.skip("data/samples/rp01.wav isn't here: batch T1's make_test_audio.py makes it")
    out = tmp_path / "clip.wav"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "0.5", "-t", str(CLIP_MS / 1000)]
        + ["-i", str(SAMPLE), "-ac", "1", "-ar", str(limits.PREPARED_SAMPLE_RATE)]
        + ["-c:a", "pcm_s16le", "-map_metadata", "-1", str(out)],
        check=True,
        timeout=60,
    )
    return out.read_bytes()


@pytest.mark.live
def test_meta_transcribes_a_two_second_clip(monkeypatch: pytest.MonkeyPatch, clip: bytes) -> None:
    # The tests pin MODEL_API_KEY empty; without that, the settings read backend/.env.
    monkeypatch.delenv("MODEL_API_KEY")
    key = Settings().MODEL_API_KEY.get_secret_value()
    if not key:
        pytest.skip("MODEL_API_KEY isn't set in backend/.env")
    stt = MetaSpeechToText(key)

    reply = stt.transcribe(clip, transcription.LANGUAGES)

    parsed = stt.parse(reply.body, CLIP_MS)
    line = (
        f"session={reply.session_id} wall_ms={reply.wall_ms} billed_ms={parsed.billed_ms}"
        f" turns={[(turn.speaker, turn.text) for turn in parsed.turns]}\n"
    )
    # For the planner, who runs it with -s. As UTF-8 bytes: on Windows, output that's piped (as
    # a tool runs it) is cp1252, and print() can't write Devanagari there.
    sys.stdout.flush()
    sys.stdout.buffer.write(line.encode("utf-8"))
    sys.stdout.buffer.flush()
    assert reply.session_id
    assert parsed.turns and all(turn.text for turn in parsed.turns)
    assert parsed.billed_ms is not None and abs(parsed.billed_ms - CLIP_MS) < 500
