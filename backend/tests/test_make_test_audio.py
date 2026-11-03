"""scripts/make_test_audio.py: role-play scripts to test recordings (batch T1).

A fake text-to-speech client stands in for Sarvam, and httpx's MockTransport for its API, so
nothing here uses the network (§3.2). The mix is real ffmpeg.
"""

import base64
import dataclasses
import hashlib
import io
import json
import math
import shutil
import struct
import wave
from pathlib import Path

import httpx
import pytest

from scripts import make_test_audio as tool

FAKE_KEY = "not-a-real-key-7Q"
RATE = tool.SAMPLE_RATE

SCRIPT = """
title = "A short test visit"

[speakers.rep]
role = "rep"
voice = "shubh"

[speakers.shopkeeper]
role = "shopkeeper"
voice = "ashutosh"

[[turns]]
speaker = "rep"
text = "नमस्ते जी, आज नया scheme है।"

[[turns]]
speaker = "shopkeeper"
text = "अच्छा, बताइए।"
pause_ms = 600

[[turns]]
speaker = "rep"
text = "दस carton पे एक free।"
pause_ms = 600
"""


def tone_wav(ms: int, frequency: float = 440.0) -> bytes:
    """A mono 16-bit WAV of a sine tone, as a TTS reply would be."""
    frames = int(RATE * ms / 1000)
    samples = (int(12_000 * math.sin(2 * math.pi * frequency * n / RATE)) for n in range(frames))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(b"".join(struct.pack("<h", s) for s in samples))
    return buffer.getvalue()


class FakeTts:
    """Speaks 20 ms per character, so turn lengths are known."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.models: list[str] = []
        self.languages: list[str] = []

    def synthesize(self, text: str, voice: str, pace: float, model: str, language: str) -> bytes:
        self.calls.append((text, voice))
        self.models.append(model)
        self.languages.append(language)
        return tone_wav(20 * len(text))


@pytest.fixture
def samples(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SARVAM_API_KEY", FAKE_KEY)  # never the developer's real key
    folder = tmp_path / "samples"
    (folder / "scripts").mkdir(parents=True)
    (folder / "scripts" / "rp01.toml").write_text(SCRIPT, encoding="utf-8")
    return folder


def run(samples: Path, *args: str, tts: FakeTts | None = None) -> int:
    return tool.main(["--samples", str(samples), *args], tts=tts)


# --- spending ----------------------------------------------------------------------------------


def test_without_yes_nothing_is_sent_and_the_cost_is_shown(
    samples: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tts = FakeTts()

    assert run(samples, "rp01", tts=tts) == 0

    assert tts.calls == []
    printed = capsys.readouterr().out
    script = tool.load_script(samples / "scripts" / "rp01.toml")
    characters = sum(len(turn.text) for turn in script.turns)
    assert f"{characters} characters" in printed
    assert "₹" in printed
    assert not (samples / "rp01.wav").exists()


def test_a_rerun_with_a_full_cache_sends_nothing(samples: Path) -> None:
    first = FakeTts()
    assert run(samples, "rp01", "--yes", tts=first) == 0
    assert len(first.calls) == 3

    again = FakeTts()
    assert run(samples, "rp01", "--yes", tts=again) == 0

    assert again.calls == []
    assert (samples / "rp01.wav").exists()


def test_a_rerun_after_adding_a_turn_sends_only_that_turn(samples: Path) -> None:
    run(samples, "rp01", "--yes", tts=FakeTts())
    script = samples / "scripts" / "rp01.toml"
    added = 'speaker = "shopkeeper"\ntext = "हाँ, ठीक है।"\n'
    script.write_text(script.read_text(encoding="utf-8") + "\n[[turns]]\n" + added, "utf-8")

    again = FakeTts()
    assert run(samples, "rp01", "--yes", tts=again) == 0

    assert again.calls == [("हाँ, ठीक है।", "ashutosh")]


def test_a_second_copy_wont_start_while_one_is_running(
    samples: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Two copies at once paid for rp07 twice and wrote over each other's files (batch T1).
    (samples / tool.LOCK_NAME).touch()
    tts = FakeTts()

    assert run(samples, "rp01", "--yes", tts=tts) == 1

    assert tts.calls == []
    assert "another copy" in capsys.readouterr().err


class RefusingTts:
    def synthesize(self, text: str, voice: str, pace: float, model: str, language: str) -> bytes:
        raise tool.StopRun("Sarvam said HTTP 401: check SARVAM_API_KEY in backend/.env")


@pytest.mark.parametrize("tts", [FakeTts(), RefusingTts()], ids=["finished", "stopped"])
def test_the_lock_is_let_go_however_the_run_ends(samples: Path, tts: object) -> None:
    run(samples, "rp01", "--yes", tts=tts)  # type: ignore[arg-type]

    assert not (samples / tool.LOCK_NAME).exists()


def test_the_cost_counts_only_what_isnt_cached(
    samples: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(samples, "rp01", "--yes", tts=FakeTts())
    capsys.readouterr()

    run(samples, "rp01")

    assert "0 characters" in capsys.readouterr().out


# --- the API -------------------------------------------------------------------------------------


def sarvam(responses: list[httpx.Response], slept: list[float]) -> tool.SarvamTts:
    pending = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["api-subscription-key"] == FAKE_KEY
        assert json.loads(request.content)["model"] == tool.DEFAULT_MODEL
        return pending.pop(0)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return tool.SarvamTts(FAKE_KEY, client=client, sleep=slept.append)


def audio_reply() -> httpx.Response:
    return httpx.Response(
        200, json={"request_id": "r1", "audios": [base64.b64encode(tone_wav(50)).decode()]}
    )


@pytest.mark.parametrize("status", [429, 500, 503])
def test_a_passing_failure_is_retried_with_backoff(status: int) -> None:
    slept: list[float] = []
    tts = sarvam([httpx.Response(status), httpx.Response(status), audio_reply()], slept)

    audio = tts.synthesize("नमस्ते", "shubh", 1.0, tool.DEFAULT_MODEL, tool.DEFAULT_LANGUAGE)

    assert audio.startswith(b"RIFF")
    assert len(slept) == 2
    assert slept[1] > slept[0]


def test_a_wait_the_api_asks_for_is_honoured() -> None:
    slept: list[float] = []
    tts = sarvam([httpx.Response(429, headers={"Retry-After": "7"}), audio_reply()], slept)

    tts.synthesize("नमस्ते", "shubh", 1.0, tool.DEFAULT_MODEL, tool.DEFAULT_LANGUAGE)

    assert slept == [7.0]


def test_a_request_that_never_left_is_retried() -> None:
    slept: list[float] = []
    outcomes: list[object] = [httpx.ConnectError("no route"), audio_reply()]

    def handler(request: httpx.Request) -> httpx.Response:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]

    client = httpx.Client(transport=httpx.MockTransport(handler))
    tool.SarvamTts(FAKE_KEY, client=client, sleep=slept.append).synthesize(
        "नमस्ते", "shubh", 1.0, tool.DEFAULT_MODEL, tool.DEFAULT_LANGUAGE
    )

    assert len(slept) == 1


def test_a_lost_reply_stops_the_run_instead_of_paying_again() -> None:
    # The request reached Sarvam, so it may already be billed (§9.3).
    sends: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sends.append(1)
        raise httpx.ReadTimeout("no reply")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    tts = tool.SarvamTts(FAKE_KEY, client=client, sleep=lambda s: None)

    with pytest.raises(tool.StopRun):
        tts.synthesize("नमस्ते", "shubh", 1.0, tool.DEFAULT_MODEL, tool.DEFAULT_LANGUAGE)

    assert sends == [1]


def test_retries_end_and_the_run_stops() -> None:
    tts = sarvam([httpx.Response(429)] * tool.MAX_TRIES, [])

    with pytest.raises(tool.StopRun):
        tts.synthesize("नमस्ते", "shubh", 1.0, tool.DEFAULT_MODEL, tool.DEFAULT_LANGUAGE)


@pytest.mark.parametrize("status", [400, 401, 402, 403, 422])
def test_a_refusal_stops_the_run_at_once_without_the_key(status: int) -> None:
    slept: list[float] = []
    body = {"error": {"code": "invalid_api_key_error", "message": f"bad key {FAKE_KEY}"}}
    tts = sarvam([httpx.Response(status, json=body)], slept)

    with pytest.raises(tool.StopRun) as stopped:
        tts.synthesize("नमस्ते", "shubh", 1.0, tool.DEFAULT_MODEL, tool.DEFAULT_LANGUAGE)

    assert slept == []
    assert str(status) in str(stopped.value)
    assert FAKE_KEY not in str(stopped.value)


def test_a_401_says_to_check_the_key(capsys: pytest.CaptureFixture[str], samples: Path) -> None:
    tts = sarvam([httpx.Response(401)], [])

    assert run(samples, "rp01", "--yes", tts=tts) == 1  # type: ignore[arg-type]

    printed = capsys.readouterr()
    assert "SARVAM_API_KEY" in printed.err
    assert FAKE_KEY not in printed.out + printed.err


@pytest.mark.parametrize(
    "reply",
    [{}, {"audios": []}, {"audios": [""]}, {"audios": ["bm90IGEgd2F2"]}],
    ids=["no-audios", "empty-list", "empty-string", "not-a-wav"],
)
def test_a_reply_without_usable_audio_stops_the_run(reply: dict) -> None:
    tts = sarvam([httpx.Response(200, json=reply)], [])

    with pytest.raises(tool.StopRun):
        tts.synthesize("नमस्ते", "shubh", 1.0, tool.DEFAULT_MODEL, tool.DEFAULT_LANGUAGE)


# --- scripts and long turns -----------------------------------------------------------------------


def test_a_turn_over_the_limit_is_split_at_sentence_ends_and_no_text_is_lost() -> None:
    text = "पहला sentence यहाँ है। दूसरा भी है। Third one is English. चौथा? हाँ, चौथा!"

    parts = tool.split_text(text, limit=30)

    assert all(len(part) <= 30 for part in parts)
    assert " ".join(parts).split() == text.split()
    assert all(part.endswith(("।", ".", "?", "!")) for part in parts)


def test_a_sentence_longer_than_the_limit_is_split_between_words() -> None:
    text = "एक बहुत लंबा sentence जिसमें कोई full stop नहीं है और फिर भी चलता रहता है"

    parts = tool.split_text(text, limit=20)

    assert all(len(part) <= 20 for part in parts)
    assert " ".join(parts).split() == text.split()


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ('[[turns]]\nspeaker = "customer"\ntext = "हाँ"\n', "customer"),
        ('[[turns]]\nspeaker = "rep"\ntext = ""\n', "empty"),
        ('[[turns]]\nspeaker = "rep"\ntext = "हाँ"\nvolume = 3\n', "volume"),
        ('[[turns]]\nspeaker = "rep"\ntext = "हाँ"\npause_ms = -5\n', "pause_ms"),
        ('[speakers.boss]\nrole = "manager"\nvoice = "dev"\n', "manager"),
        ('[[turns]]\nspeaker = "rep"\ntext = "हाँ"\npause_ms = true\n', "pause_ms"),
        ('[speakers.boss]\nrole = "other"\nvoice = "dev"\npace = "fast"\n', "pace"),
        ('[speakers.boss]\nrole = "other"\nvoice = "dev"\npace = 3\n', "pace"),
        ('noise_db = "loud"\n', "noise_db"),
        ("noise_db = 20\n", "noise_db"),
    ],
    ids=[
        "unknown-speaker",
        "empty-text",
        "unknown-field",
        "negative-pause",
        "unknown-role",
        "boolean-pause",
        "text-pace",
        "pace-too-fast",
        "text-noise",
        "noise-above-0-db",
    ],
)
def test_a_script_with_a_mistake_is_refused_clearly(
    tmp_path: Path, change: str, problem: str
) -> None:
    path = tmp_path / "rp99.toml"
    # A top-level key goes before the first table; a table goes at the end.
    top_level = not change.startswith("[")
    path.write_text(change + SCRIPT if top_level else SCRIPT + "\n" + change, encoding="utf-8")

    with pytest.raises(tool.ScriptError) as error:
        tool.load_script(path)

    assert "rp99.toml" in str(error.value)
    assert problem in str(error.value)
    assert "unknown fields" not in str(error.value) or problem == "volume"


def test_a_name_without_a_script_stops_with_one_line(
    samples: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(samples, "rp42", tts=FakeTts()) == 1

    assert "rp42" in capsys.readouterr().err


def test_recording_in_chrome_before_the_recording_exists_stops_with_one_line(
    samples: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(samples, "--chrome", "rp01", "--chrome-path", "no-such-chrome") == 1

    assert "rp01" in capsys.readouterr().err


def test_the_same_text_is_counted_and_sent_once(samples: Path) -> None:
    script = samples / "scripts" / "rp01.toml"
    repeat = '\n[[turns]]\nspeaker = "rep"\ntext = "जी हाँ।"\n'
    script.write_text(script.read_text(encoding="utf-8") + repeat * 3, encoding="utf-8")
    loaded = tool.load_script(script)
    unique = {(turn.speaker, turn.text) for turn in loaded.turns}
    tts = FakeTts()

    assert tool.characters_to_send([loaded], samples / "tts_cache") == sum(
        len(text) for _, text in unique
    )
    run(samples, "rp01", "--yes", tts=tts)
    assert len(tts.calls) == len(unique)


def test_both_readers_of_the_dev_tool_key_read_it_the_same_way() -> None:
    from scripts.check_setup import DevToolKeys

    assert tool._Keys.model_fields.keys() == DevToolKeys.model_fields.keys()
    assert tool._Keys.model_config["env_file"] == DevToolKeys.model_config["env_file"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="the mix needs ffmpeg")
def test_a_long_script_mixes_without_running_into_a_command_line_limit(samples: Path) -> None:
    # Windows refuses a command line over 32,767 characters: long visits must still mix. The
    # shopkeeper's turns have the longest filters (the room's echo).
    turns = "".join(f'\n[[turns]]\nspeaker = "shopkeeper"\ntext = "मोड़ {n}"\n' for n in range(280))
    script = samples / "scripts" / "rp01.toml"
    script.write_text(script.read_text(encoding="utf-8") + turns, encoding="utf-8")

    assert run(samples, "rp01", "--yes", tts=FakeTts()) == 0

    assert len(json.loads((samples / "rp01.turns.json").read_text("utf-8"))["turns"]) == 283


# --- the mix -------------------------------------------------------------------------------------


def window_rms(samples: list[int], start_ms: int, end_ms: int) -> float:
    chunk = samples[int(RATE * start_ms / 1000) : int(RATE * end_ms / 1000)]
    return math.sqrt(sum(s * s for s in chunk) / max(len(chunk), 1))


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="the mix needs ffmpeg")
def test_the_mix_has_each_turn_where_turns_json_says(samples: Path) -> None:
    assert run(samples, "rp01", "--yes", tts=FakeTts()) == 0

    turns = json.loads((samples / "rp01.turns.json").read_text(encoding="utf-8"))
    with wave.open(str(samples / "rp01.wav"), "rb") as mix:
        assert (mix.getnchannels(), mix.getframerate()) == (1, RATE)
        samples_ = list(struct.unpack(f"<{mix.getnframes()}h", mix.readframes(mix.getnframes())))
    script = tool.load_script(samples / "scripts" / "rp01.toml")
    assert [t["text"] for t in turns["turns"]] == [turn.text for turn in script.turns]
    for turn in turns["turns"]:
        assert turn["end_ms"] - turn["start_ms"] == 20 * len(turn["text"])
        # Sound just after the turn starts, and silence just before it.
        assert window_rms(samples_, turn["start_ms"] + 30, turn["start_ms"] + 80) > 1_000
        assert window_rms(samples_, turn["start_ms"] - 120, turn["start_ms"] - 20) < 50
    assert (samples / "rp01.m4a").exists()
    assert turns["duration_ms"] >= turns["turns"][-1]["end_ms"]


# --- the model a script names (batch T2) ------------------------------------------------------

V4 = 'model = "bulbul:v4-flash"\n'


def with_model(samples: Path, line: str) -> Path:
    script = samples / "scripts" / "rp01.toml"
    script.write_text(line + script.read_text(encoding="utf-8"), encoding="utf-8")
    return script


def test_a_script_naming_no_model_is_bulbul_v3_as_before(samples: Path) -> None:
    loaded = tool.load_script(samples / "scripts" / "rp01.toml")
    [first, *_] = tool.requests_for(loaded)[0]
    as_before = {"model": "bulbul:v3", "language": "hi-IN", "voice": first.voice}
    as_before |= {"pace": first.pace, "sample_rate": tool.SAMPLE_RATE, "text": first.text}
    hashed = hashlib.sha256(json.dumps(as_before, sort_keys=True).encode()).hexdigest()

    assert loaded.model == "bulbul:v3"
    assert first.cache_name() == f"{hashed}.wav"  # rp01–rp10's cached replies still fit


def test_bulbul_v4_flash_is_sent_and_cached_apart(samples: Path) -> None:
    script = with_model(samples, V4)
    loaded = tool.load_script(script)
    first = tool.requests_for(loaded)[0][0]
    tts = FakeTts()

    run(samples, "rp01", "--yes", tts=tts)

    assert loaded.model == "bulbul:v4-flash"
    assert set(tts.models) == {"bulbul:v4-flash"}
    v3 = dataclasses.replace(first, model=tool.DEFAULT_MODEL)
    assert first.cache_name() != v3.cache_name()


@pytest.mark.parametrize(
    ("value", "shown"),
    [('"bulbul:v9"', "'bulbul:v9'"), ('["bulbul:v4-flash"]', "['bulbul:v4-flash']")],
)
def test_an_unknown_model_is_refused(samples: Path, value: str, shown: str) -> None:
    script = with_model(samples, f"model = {value}\n")

    with pytest.raises(tool.ScriptError) as refused:
        tool.load_script(script)

    assert str(refused.value) == f"rp01.toml: model {shown} isn't one of bulbul:v3, bulbul:v4-flash"


@pytest.mark.parametrize(("line", "rate"), [("", 3.00), (V4, 1.00)])
def test_the_estimate_is_at_the_model_s_price(
    samples: Path, capsys: pytest.CaptureFixture[str], line: str, rate: float
) -> None:
    with_model(samples, line)
    loaded = tool.load_script(samples / "scripts" / "rp01.toml")
    characters = tool.characters_to_send([loaded], samples / "tts_cache")

    run(samples, "rp01")

    total = capsys.readouterr().out.splitlines()[1]
    assert total == (
        f"total: {characters} characters to send, about ₹{characters / 1000 * rate:.2f} before GST"
        f" ({loaded.model} at ₹{rate:.2f} per 1,000)"
    )


def test_scripts_on_both_models_are_each_priced_at_their_own(
    samples: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    v4 = samples / "scripts" / "rp02.toml"
    v4.write_text(V4 + SCRIPT, encoding="utf-8")
    loaded = tool.load_script(v4)
    characters = tool.characters_to_send([loaded], samples / "tts_cache")

    run(samples, "rp01", "rp02")

    # The same lines on two models are two requests each, at ₹3.00 and ₹1.00 per 1,000.
    total = capsys.readouterr().out.splitlines()[2]
    assert total == (
        f"total: {2 * characters} characters to send, about ₹{characters / 1000 * 4:.2f} before GST"
        " (bulbul:v3 at ₹3.00 per 1,000, bulbul:v4-flash at ₹1.00 per 1,000)"
    )


def test_a_cached_script_s_model_isn_t_among_the_rates(
    samples: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(samples, "rp01", "--yes", tts=FakeTts())  # rp01, on v3, all cached
    v4 = samples / "scripts" / "rp02.toml"
    v4.write_text(V4 + SCRIPT, encoding="utf-8")
    characters = tool.characters_to_send([tool.load_script(v4)], samples / "tts_cache")
    capsys.readouterr()

    run(samples, "rp01", "rp02")

    total = capsys.readouterr().out.splitlines()[2]
    assert total == (
        f"total: {characters} characters to send, about ₹{characters / 1000:.2f} before GST"
        " (bulbul:v4-flash at ₹1.00 per 1,000)"
    )


def test_the_api_is_sent_the_script_s_model(samples: Path) -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["model"])
        return httpx.Response(200, json={"audios": [base64.b64encode(tone_wav(100)).decode()]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    tool.SarvamTts(FAKE_KEY, client=client).synthesize(
        "नमस्ते", "kavya_hi_conversational", 1.0, "bulbul:v4-flash", "hi-IN"
    )

    assert sent == ["bulbul:v4-flash"]


# --- the language a script names (batch T3) ---------------------------------------------------

MARATHI = 'language = "mr-IN"\n'


def test_a_script_naming_no_language_is_hindi(samples: Path) -> None:
    loaded = tool.load_script(samples / "scripts" / "rp01.toml")

    assert loaded.language == "hi-IN"
    assert tool.requests_for(loaded)[0][0].language == "hi-IN"


def test_marathi_is_sent_and_cached_apart(samples: Path) -> None:
    loaded = tool.load_script(with_model(samples, MARATHI))
    first = tool.requests_for(loaded)[0][0]
    tts = FakeTts()

    run(samples, "rp01", "--yes", tts=tts)

    assert set(tts.languages) == {"mr-IN"}
    hindi = dataclasses.replace(first, language=tool.DEFAULT_LANGUAGE)
    assert first.cache_name() != hindi.cache_name()


@pytest.mark.parametrize(("value", "shown"), [('"ta-IN"', "'ta-IN'"), ("1", "1")])
def test_an_unknown_language_is_refused(samples: Path, value: str, shown: str) -> None:
    script = with_model(samples, f"language = {value}\n")

    with pytest.raises(tool.ScriptError) as refused:
        tool.load_script(script)

    assert str(refused.value) == f"rp01.toml: language {shown} isn't one of hi-IN, mr-IN"


def test_the_api_is_sent_the_script_s_language(samples: Path) -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["target_language_code"])
        return httpx.Response(200, json={"audios": [base64.b64encode(tone_wav(100)).decode()]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    tool.SarvamTts(FAKE_KEY, client=client).synthesize(
        "नमस्कार", "ritu_mr_conversational", 1.0, "bulbul:v4-flash", "mr-IN"
    )

    assert sent == ["mr-IN"]
