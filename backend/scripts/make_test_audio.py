"""Makes K1's test recordings from the role-play scripts: a dev tool (batch T1, BRIEF D15).

    uv run python scripts/make_test_audio.py               # what it would send, and the cost
    uv run python scripts/make_test_audio.py rp01 --yes    # make rp01
    uv run python scripts/make_test_audio.py --yes         # make every script
    uv run python scripts/make_test_audio.py --chrome rp09 # record rp09.wav through Chrome

- Scripts are data/samples/scripts/rpNN.toml at the repo root, git-ignored like all of data/.
- Each turn goes to Sarvam's text-to-speech (Bulbul): bulbul:v3, or the script's own `model`
  (batch T2: bulbul:v4-flash, whose voices are persona IDs such as kavya_hi_conversational),
  in Hindi or the script's own `language` (batch T3: "mr-IN", Marathi; "en-IN", English).
  Each reply is cached under
  data/samples/tts_cache/, keyed by everything the request sends, so a rerun pays only for what's
  missing (§9.4). Without --yes nothing is sent: it prints the characters and the cost.
- ffmpeg mixes the turns into rpNN.wav (the master), rpNN.m4a (like a phone's voice recorder)
  and rpNN.turns.json (who said what, and when, in the mix).
- Test data only: the app never calls Sarvam, and test_architecture.py keeps it out of
  countertalk/. The key is SARVAM_API_KEY in backend/.env, and it's never printed.
"""

import argparse
import base64
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import wave
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from countertalk.config import BACKEND_DIR
from countertalk.logs import configure_logging

SAMPLES_DIR = BACKEND_DIR.parent / "data" / "samples"

# Sarvam's text-to-speech, as its docs gave it on 1 Nov 2026.
API_URL = "https://api.sarvam.ai/text-to-speech"
# The models a script may name (`model`), and what each costs: ₹ per 1,000 characters, pay as
# you go, before GST, from Sarvam's pricing page (read 3 Nov 2026). v4-flash has "the same
# request and response contract as bulbul:v3" (its docs, 3 Nov).
DEFAULT_MODEL = "bulbul:v3"
RUPEES_PER_1000_CHARS = {"bulbul:v3": 3.00, "bulbul:v4-flash": 1.00}
# The languages a script may be spoken in (`language`, batch T3).
DEFAULT_LANGUAGE = "hi-IN"
LANGUAGES = ("hi-IN", "mr-IN", "en-IN")
MAX_CHARS_PER_REQUEST = 2_500
SAMPLE_RATE = 22_050  # Sarvam's default, kept through the mix
REQUEST_TIMEOUT_S = 60
MAX_TRIES = 5
FIRST_BACKOFF_S = 2.0
MAX_RETRY_AFTER_S = 120
# Rate limits and server trouble are retried; any other failure is a refusal and stops the run.
PASSING_STATUSES = {429, 500, 502, 503, 504}

ROLES = ("rep", "shopkeeper", "other")
PACE_RANGE = (0.5, 2.0)  # bulbul:v3's and bulbul:v4-flash's (Sarvam's docs, 3 Nov)
# The rep holds the phone; the shopkeeper and anyone else are farther off, in the room's echo.
ROLE_GAIN_DB = {"rep": 0.0, "shopkeeper": -4.0, "other": -6.0}
ROOM_ECHO = "aecho=0.8:0.85:40|75:0.25|0.12"
GAP_MS = (200, 1_000)
LEAD_IN_MS = 500
TAIL_MS = 800
PART_PAUSE_MS = 150  # between the pieces of a turn split for the API's limit
MAX_TURNS = 300  # one ffmpeg command line holds about 330 turns' inputs and filters
PEAK_LIMIT = 0.89  # about -1 dBFS, so the master never clips
FFMPEG_TIMEOUT_S = 300
CHROME_SEGMENT_MS = 30_000
LOCK_NAME = ".make_test_audio.lock"
CHROME_CANDIDATES = (
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "google-chrome",
    "chromium",
)
SENTENCE_END = re.compile(r"(?<=[।.?!])\s+")


class _Keys(BaseSettings):
    # As scripts/check_setup.py's DevToolKeys: scripts don't import one another.
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")

    SARVAM_API_KEY: SecretStr = SecretStr("")


class ScriptError(Exception):
    """A script that can't be used as written. The message names the file and the problem."""


class StopRun(Exception):
    """Something only a person can fix, such as a refused key. Never holds the key."""


@dataclass(frozen=True)
class Speaker:
    role: str
    voice: str
    pace: float = 1.0


@dataclass(frozen=True)
class Turn:
    speaker: str
    text: str
    pause_ms: int | None = None
    overlap_ms: int | None = None


@dataclass(frozen=True)
class Script:
    name: str
    title: str
    speakers: dict[str, Speaker]
    turns: list[Turn]
    noise_db: float | None = None
    model: str = DEFAULT_MODEL
    language: str = DEFAULT_LANGUAGE


def load_script(path: Path) -> Script:
    """Reads one rpNN.toml, refusing anything it doesn't expect (§9.6)."""

    def problem(text: str) -> ScriptError:
        return ScriptError(f"{path.name}: {text}")

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise problem("there's no such script") from None
    except tomllib.TOMLDecodeError as error:
        raise problem(f"isn't valid TOML ({error})") from None
    known = {"title", "model", "language", "noise_db", "speakers", "turns"}
    _only(data, known, problem, "the script")
    model = data.get("model", DEFAULT_MODEL)
    if not isinstance(model, str) or model not in RUPEES_PER_1000_CHARS:
        raise problem(f"model {model!r} isn't one of {', '.join(RUPEES_PER_1000_CHARS)}")
    language = data.get("language", DEFAULT_LANGUAGE)
    if not isinstance(language, str) or language not in LANGUAGES:
        raise problem(f"language {language!r} isn't one of {', '.join(LANGUAGES)}")
    speakers = {}
    for key, value in dict(data.get("speakers", {})).items():
        _only(value, {"role", "voice", "pace"}, problem, f"speaker {key}")
        if value.get("role") not in ROLES:
            raise problem(f"speaker {key}'s role {value.get('role')!r} isn't one of {ROLES}")
        if not isinstance(value.get("voice"), str) or not value["voice"]:
            raise problem(f"speaker {key} needs a voice")
        pace = value.get("pace", 1.0)
        if not _number(pace) or not PACE_RANGE[0] <= pace <= PACE_RANGE[1]:
            raise problem(
                f"speaker {key}'s pace must be a number from {PACE_RANGE[0]} to {PACE_RANGE[1]}"
            )
        speakers[key] = Speaker(value["role"], value["voice"], float(pace))
    turns = []
    for number, value in enumerate(data.get("turns", []), start=1):
        where = f"turn {number}"
        _only(value, {"speaker", "text", "pause_ms", "overlap_ms"}, problem, where)
        if value.get("speaker") not in speakers:
            raise problem(f"{where}'s speaker {value.get('speaker')!r} isn't in [speakers]")
        if not isinstance(value.get("text"), str) or not value["text"].strip():
            raise problem(f"{where}'s text is empty")
        for field in ("pause_ms", "overlap_ms"):
            if field in value and not (_number(value[field], whole=True) and value[field] >= 0):
                raise problem(f"{where}'s {field} must be a whole number, 0 or more")
        turns.append(
            Turn(
                value["speaker"],
                value["text"].strip(),
                value.get("pause_ms"),
                value.get("overlap_ms"),
            )
        )
    if not turns:
        raise problem("has no turns")
    noise = data.get("noise_db")
    if noise is not None and not (_number(noise) and noise <= 0):
        raise problem("noise_db must be a number of dB, 0 or less")
    return Script(
        path.stem,
        str(data.get("title", "")),
        speakers,
        turns,
        None if noise is None else float(noise),
        model,
        language,
    )


def _number(value: object, *, whole: bool = False) -> bool:
    # TOML's true is a bool, and Python's bool is an int: neither is a number here.
    kinds = (int,) if whole else (int, float)
    return isinstance(value, kinds) and not isinstance(value, bool)


def _only(table: Any, allowed: set[str], problem: Callable[[str], ScriptError], where: str) -> None:
    if not isinstance(table, dict):
        raise problem(f"{where} isn't a table")
    if unknown := sorted(set(table) - allowed):
        raise problem(f"{where} has unknown fields: {', '.join(unknown)}")


def split_text(text: str, limit: int = MAX_CHARS_PER_REQUEST) -> list[str]:
    """Pieces of at most `limit` characters: cut at sentence ends where it can, between words
    where it must. Together the pieces hold every word, in order."""
    pieces: list[str] = []
    for sentence in SENTENCE_END.split(text.strip()):
        for part in _fit(sentence, limit):
            if pieces and len(pieces[-1]) + 1 + len(part) <= limit:
                pieces[-1] = f"{pieces[-1]} {part}"
            else:
                pieces.append(part)
    return pieces


def _fit(sentence: str, limit: int) -> list[str]:
    parts: list[str] = []
    for word in sentence.split():
        while len(word) > limit:  # a single "word" over the limit is cut where it must be
            parts.append(word[:limit])
            word = word[limit:]
        if parts and len(parts[-1]) + 1 + len(word) <= limit:
            parts[-1] = f"{parts[-1]} {word}"
        else:
            parts.append(word)
    return parts


@dataclass(frozen=True)
class Request:
    text: str
    voice: str
    pace: float
    model: str
    language: str

    def cache_name(self) -> str:
        # A v3 Hindi request's name is as it was before models and languages could be chosen:
        # rp01–rp10's cache holds.
        sent = {
            "model": self.model,
            "language": self.language,
            "voice": self.voice,
            "pace": self.pace,
            "sample_rate": SAMPLE_RATE,
            "text": self.text,
        }
        return hashlib.sha256(json.dumps(sent, sort_keys=True).encode()).hexdigest() + ".wav"


def requests_for(script: Script) -> list[list[Request]]:
    """Each turn's requests: one, or several if it's over the API's limit."""
    return [
        [
            Request(
                part,
                script.speakers[turn.speaker].voice,
                script.speakers[turn.speaker].pace,
                script.model,
                script.language,
            )
            for part in split_text(turn.text)
        ]
        for turn in script.turns
    ]


def _missing(scripts: list[Script], cache: Path) -> dict[str, Request]:
    """What isn't cached yet, each distinct request once, as fetch_missing will send it."""
    return {
        request.cache_name(): request
        for script in scripts
        for parts in requests_for(script)
        for request in parts
        if not (cache / request.cache_name()).exists()
    }


def characters_to_send(scripts: list[Script], cache: Path) -> int:
    return sum(len(request.text) for request in _missing(scripts, cache).values())


def cost_rupees(requests: Iterable[Request]) -> float:
    """What sending these requests costs, before GST, each at its own model's price."""
    return sum(
        len(request.text) / 1000 * RUPEES_PER_1000_CHARS[request.model] for request in requests
    )


class Tts(Protocol):
    def synthesize(
        self, text: str, voice: str, pace: float, model: str, language: str
    ) -> bytes: ...


class SarvamTts:
    """Sarvam's text-to-speech. Retries passing failures with backoff; stops on a refusal.

    A request that never left (no connection) is retried. One whose reply was lost may already
    be billed, so the run stops instead of paying again (§9.3); a rerun sends only what's missing.
    """

    def __init__(
        self,
        key: str,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._key = key
        self._client = client or httpx.Client()
        self._sleep = sleep

    def synthesize(self, text: str, voice: str, pace: float, model: str, language: str) -> bytes:
        body = {
            "text": text,
            "target_language_code": language,
            "speaker": voice,
            "model": model,
            "pace": pace,
            "speech_sample_rate": SAMPLE_RATE,
        }
        for attempt in range(1, MAX_TRIES + 1):
            try:
                response = self._client.post(
                    API_URL,
                    json=body,
                    headers={"api-subscription-key": self._key},
                    timeout=REQUEST_TIMEOUT_S,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                outcome, asked_s = type(error).__name__, None
            except httpx.TransportError as error:
                raise StopRun(
                    f"Sarvam's reply was lost ({type(error).__name__}) after the request was sent,"
                    " so it may be billed already; run again to send only what's missing"
                ) from None
            else:
                if response.status_code == 200:
                    return _audio(response)
                if response.status_code not in PASSING_STATUSES:
                    raise StopRun(_refusal(response))
                outcome, asked_s = f"HTTP {response.status_code}", _retry_after_s(response)
            if attempt < MAX_TRIES:
                wait_s = asked_s if asked_s is not None else FIRST_BACKOFF_S * 2 ** (attempt - 1)
                print(f"  Sarvam: {outcome}; trying again in {wait_s:.0f} s", file=sys.stderr)
                self._sleep(wait_s)
        raise StopRun(f"Sarvam kept failing ({outcome}) after {MAX_TRIES} tries; try later")


def _retry_after_s(response: httpx.Response) -> float | None:
    """The wait a 429 or 503 asks for, in whole seconds, within reason."""
    value = response.headers.get("Retry-After", "")
    return (
        float(min(int(value), MAX_RETRY_AFTER_S)) if value.isascii() and value.isdigit() else None
    )


def _audio(response: httpx.Response) -> bytes:
    try:
        audio = base64.b64decode(response.json()["audios"][0], validate=True)
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise StopRun(f"Sarvam's reply had no usable audio ({type(error).__name__})") from None
    if not audio.startswith(b"RIFF"):
        raise StopRun("Sarvam's reply wasn't a WAV file")
    return audio


def _refusal(response: httpx.Response) -> str:
    status = response.status_code
    hint = {
        401: "check SARVAM_API_KEY in backend/.env",
        403: "check SARVAM_API_KEY in backend/.env",
        402: "the account is out of credits",
    }.get(status, "the request was refused")
    code = ""
    try:
        code = str(response.json()["error"]["code"])
    except (ValueError, KeyError, TypeError):
        pass
    # Only a plain error code, never the message: it could echo what was sent.
    shown = f" ({code})" if re.fullmatch(r"[a-z_]{1,64}", code) else ""
    return f"Sarvam said HTTP {status}{shown}: {hint}"


def fetch_missing(script: Script, cache: Path, tts: Tts) -> int:
    """Sends what isn't cached, saving each reply as it arrives. Returns the characters sent."""
    cache.mkdir(parents=True, exist_ok=True)
    sent = 0
    for parts in requests_for(script):
        for request in parts:
            path = cache / request.cache_name()
            if path.exists():
                continue
            audio = tts.synthesize(
                request.text, request.voice, request.pace, request.model, request.language
            )
            partial = path.with_suffix(".part")
            partial.write_bytes(audio)
            os.replace(partial, path)
            sent += len(request.text)
    return sent


@dataclass(frozen=True)
class Placed:
    speaker: str
    role: str
    text: str
    start_ms: int
    end_ms: int


def place(script: Script, lengths_ms: list[int]) -> list[Placed]:
    """Where each turn goes in the mix: after a natural gap, or overlapping the one before."""
    gaps = random.Random(script.name)  # the same mix on every run
    placed: list[Placed] = []
    latest_end = LEAD_IN_MS
    for turn, length in zip(script.turns, lengths_ms, strict=True):
        if not placed:
            start = LEAD_IN_MS
        elif turn.overlap_ms is not None:
            start = max(0, placed[-1].end_ms - turn.overlap_ms)
        else:
            pause = turn.pause_ms if turn.pause_ms is not None else gaps.randint(*GAP_MS)
            start = latest_end + pause
        speaker = script.speakers[turn.speaker]
        placed.append(Placed(turn.speaker, speaker.role, turn.text, start, start + length))
        latest_end = max(latest_end, start + length)
    return placed


def wav_ms(path: Path) -> int:
    try:
        with wave.open(str(path), "rb") as audio:
            return round(audio.getnframes() * 1000 / audio.getframerate())
    except (wave.Error, EOFError) as error:
        raise StopRun(f"{path.name} isn't a WAV this tool can read ({error})") from None


def _join_parts(parts: list[Path], out: Path) -> Path:
    with wave.open(str(out), "wb") as joined:
        for index, part in enumerate(parts):
            with wave.open(str(part), "rb") as piece:
                if index == 0:
                    joined.setparams(piece.getparams())
                else:
                    silence = int(piece.getframerate() * PART_PAUSE_MS / 1000)
                    joined.writeframes(
                        b"\0" * silence * piece.getsampwidth() * piece.getnchannels()
                    )
                joined.writeframes(piece.readframes(piece.getnframes()))
    return out


def _ffmpeg(args: list[str], cwd: Path | None = None) -> None:
    if shutil.which("ffmpeg") is None:
        raise StopRun("ffmpeg isn't installed")
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=FFMPEG_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise StopRun(f"ffmpeg took longer than {FFMPEG_TIMEOUT_S} s") from None
    if result.returncode != 0:
        lines = [line for line in result.stderr.splitlines() if line and not line[0].isspace()]
        raise StopRun("ffmpeg failed: " + " | ".join(lines[-3:]))


def render(script: Script, placed: list[Placed], folder: Path, out: Path) -> int:
    """Mixes the turns, folder/t000.wav and on, into one mono WAV and returns its length.

    ffmpeg runs in that folder with short names: Windows refuses a command line over 32,767
    characters, and each turn adds its input and its filter to it.
    """
    if len(placed) > MAX_TURNS:
        raise StopRun(f"{script.name} has {len(placed)} turns; split it, {MAX_TURNS} at most")
    duration_s = (max(turn.end_ms for turn in placed) + TAIL_MS) / 1000
    inputs: list[str] = []
    chains: list[str] = []
    for index, turn in enumerate(placed):
        inputs += ["-i", f"t{index:03d}.wav"]
        room = "" if turn.role == "rep" else f",{ROOM_ECHO}"
        chains.append(
            f"[{index}:a]volume={ROLE_GAIN_DB[turn.role]}dB{room},"
            f"adelay={turn.start_ms}:all=1[t{index}]"
        )
    labels = "".join(f"[t{index}]" for index in range(len(placed)))
    if script.noise_db is not None:
        noise = f"anoisesrc=color=brown:amplitude=1:sample_rate={SAMPLE_RATE}:duration={duration_s}"
        inputs += ["-f", "lavfi", "-i", noise]
        chains.append(f"[{len(placed)}:a]lowpass=f=1200,volume={script.noise_db}dB[noise]")
        labels += "[noise]"
    count = labels.count("[")
    chains.append(
        f"{labels}amix=inputs={count}:normalize=0:dropout_transition=0,"
        f"apad=whole_dur={duration_s},atrim=end={duration_s},"
        f"alimiter=limit={PEAK_LIMIT}:level=0[out]"
    )
    partial = out.with_name(out.stem + ".part.wav")
    _ffmpeg(
        [*inputs, "-filter_complex", ";".join(chains), "-map", "[out]", "-ac", "1"]
        + ["-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(partial.resolve())],
        cwd=folder,
    )
    os.replace(partial, out)
    return round(duration_s * 1000)


def make(script: Script, samples: Path, tts: Tts | None) -> int:
    """Makes rpNN.wav, rpNN.m4a and rpNN.turns.json. Every turn must be cached or fetchable."""
    cache = samples / "tts_cache"
    if tts is not None:
        fetch_missing(script, cache, tts)
    with tempfile.TemporaryDirectory() as scratch:
        folder = Path(scratch)
        for index, parts in enumerate(requests_for(script)):
            files = [cache / request.cache_name() for request in parts]
            _join_parts(files, folder / f"t{index:03d}.wav")
        lengths = [wav_ms(folder / f"t{index:03d}.wav") for index in range(len(script.turns))]
        placed = place(script, lengths)
        duration_ms = render(script, placed, folder, samples / f"{script.name}.wav")
    _ffmpeg(
        ["-i", str(samples / f"{script.name}.wav"), "-c:a", "aac", "-b:a", "64k", "-ac", "1"]
        + [str(samples / f"{script.name}.m4a")]
    )
    turns = {
        "recording": script.name,
        "title": script.title,
        "duration_ms": duration_ms,
        "speakers": {key: {"role": s.role, "voice": s.voice} for key, s in script.speakers.items()},
        "turns": [vars(turn) for turn in placed],
    }
    (samples / f"{script.name}.turns.json").write_text(
        json.dumps(turns, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return duration_ms


RECORDER_PAGE = """<!doctype html><meta charset="utf-8"><title>recorder</title><script>
const ms = Number(new URLSearchParams(location.search).get("ms"));
let sending = Promise.resolve();
let next = 0;
navigator.mediaDevices.getUserMedia({audio: true}).then((stream) => {
  const recorder = new MediaRecorder(stream, {mimeType: "audio/webm;codecs=opus"});
  recorder.ondataavailable = (event) => {
    const seq = next++;
    sending = sending.then(() => fetch(`/segment/${seq}`, {method: "POST", body: event.data}));
  };
  recorder.onstop = () => sending.then(() => fetch("/done", {method: "POST"}));
  recorder.start(SEGMENT_MS);
  setTimeout(() => recorder.stop(), ms);
}).catch((error) => fetch("/failed", {method: "POST", body: String(error)}));
</script>""".replace("SEGMENT_MS", str(CHROME_SEGMENT_MS))


def record_in_chrome(name: str, samples: Path, chrome: str) -> int:
    """Plays rpNN.wav into headless Chrome's fake microphone and keeps MediaRecorder's 30 s
    blobs, as batch 7's browser will upload them. Returns how many segments it saved."""
    master, turns_file = samples / f"{name}.wav", samples / f"{name}.turns.json"
    if not (master.exists() and turns_file.exists()):
        raise StopRun(f"make {name} first: {master.name} or {turns_file.name} is missing")
    record_ms = json.loads(turns_file.read_text(encoding="utf-8"))["duration_ms"] + 2_000
    segments: dict[int, bytes] = {}
    finished = threading.Event()
    failure: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
            self._reply(RECORDER_PAGE.encode(), "text/html")

        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path.startswith("/segment/"):
                segments[int(self.path.rsplit("/", 1)[1])] = body
            if self.path == "/failed":
                failure.append(body.decode(errors="replace"))
            if self.path in ("/done", "/failed"):
                finished.set()
            self._reply(b"", "text/plain")

        def _reply(self, body: bytes, kind: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # Chrome's child processes can hold the profile a moment after it exits (Windows).
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as scratch:
        capture = Path(scratch) / "capture.wav"
        _ffmpeg(["-i", str(master), "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(capture)])
        url = f"http://127.0.0.1:{server.server_address[1]}/?ms={record_ms}"
        flags = ["--headless=new", "--no-first-run", "--disable-gpu"]
        flags += ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"]
        flags += [f"--use-file-for-fake-audio-capture={capture}%noloop"]
        flags += [f"--user-data-dir={Path(scratch) / 'profile'}"]
        try:
            browser = subprocess.Popen(
                [chrome, *flags, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except OSError as error:
            server.shutdown()
            raise StopRun(f"Chrome didn't start ({type(error).__name__}): {chrome}") from None
        try:
            done = finished.wait(record_ms / 1000 + 120)
        finally:
            browser.terminate()
            try:
                browser.wait(30)
            except subprocess.TimeoutExpired:
                browser.kill()
            server.shutdown()
        if not done:
            raise StopRun("Chrome didn't finish recording in time")
        if failure:
            raise StopRun(f"Chrome couldn't record: {failure[0][:200]}")
        if not segments or sorted(segments) != list(range(len(segments))):
            raise StopRun(f"Chrome's segments arrived incomplete: {sorted(segments)}")
        # Saved before the profile is cleaned up, so nothing there can lose the recording.
        folder = samples / f"{name}.chrome"
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir()
        for seq, body in sorted(segments.items()):
            (folder / f"segment_{seq:03d}.bin").write_bytes(body)
        joined = b"".join(segments[seq] for seq in sorted(segments))
        (samples / f"{name}.chrome.webm").write_bytes(joined)
    return len(segments)


def _chrome() -> str:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists() or shutil.which(candidate):
            return candidate
    raise StopRun("Chrome isn't installed; pass --chrome-path")


class _OneAtATime:
    """A lock file in the samples folder: two runs at once would pay twice and write over each
    other's files. A crash can leave it behind, and the message says how to clear it."""

    def __init__(self, samples: Path) -> None:
        self._path = samples / LOCK_NAME

    def __enter__(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            os.close(os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            raise StopRun(f"another copy is running; if none is, delete {self._path}") from None

    def __exit__(self, *args: object) -> None:
        self._path.unlink(missing_ok=True)


def main(argv: list[str], *, tts: Tts | None = None) -> int:
    parser = argparse.ArgumentParser(description="Makes the test recordings (batch T1).")
    parser.add_argument("names", nargs="*", help="rp01 rp02 ...; every script if none")
    parser.add_argument("--yes", action="store_true", help="send what isn't cached, and pay")
    parser.add_argument("--chrome", metavar="NAME", help="record NAME's master through Chrome")
    parser.add_argument("--chrome-path", help="Chrome's executable, if not the usual place")
    parser.add_argument("--samples", type=Path, default=SAMPLES_DIR, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    samples: Path = args.samples
    try:
        if args.chrome:
            with _OneAtATime(samples):
                count = record_in_chrome(args.chrome, samples, args.chrome_path or _chrome())
            print(f"{args.chrome}: {count} segments in {args.chrome}.chrome/, and the joined .webm")
            return 0
        names = args.names or sorted(path.stem for path in (samples / "scripts").glob("rp*.toml"))
        scripts = [load_script(samples / "scripts" / f"{name}.toml") for name in names]
        if not scripts:
            raise StopRun(f"no scripts in {samples / 'scripts'}")
        cache = samples / "tts_cache"
        for script in scripts:
            characters = characters_to_send([script], cache)
            print(
                f"{script.name}: {len(script.turns)} turns, {characters} characters to send"
                f" ({script.model}, {script.language})"
            )
        missing = _missing(scripts, cache).values()
        total = sum(len(request.text) for request in missing)
        # The rates of what's still to send: a fully cached script's model costs nothing.
        used = sorted({request.model for request in missing} or {s.model for s in scripts})
        rate = ", ".join(f"{m} at ₹{RUPEES_PER_1000_CHARS[m]:.2f} per 1,000" for m in used)
        rupees = cost_rupees(missing)
        print(f"total: {total} characters to send, about ₹{rupees:.2f} before GST ({rate})")
        if total and not args.yes:
            print("Nothing was sent. Run again with --yes to send it and pay.")
            return 0
        if total and tts is None:
            key = _Keys().SARVAM_API_KEY.get_secret_value()
            if not key:
                raise StopRun("SARVAM_API_KEY isn't set in backend/.env")
            tts = SarvamTts(key)
        with _OneAtATime(samples):
            for script in scripts:
                duration_ms = make(script, samples, tts if total else None)
                print(f"{script.name}: {duration_ms / 1000:.1f} s, with .m4a and .turns.json")
    except (ScriptError, StopRun) as error:
        print(f"stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    configure_logging()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # for ₹, even when piped
    sys.exit(main(sys.argv[1:]))
