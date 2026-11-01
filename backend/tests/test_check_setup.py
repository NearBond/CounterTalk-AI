import sys
import tomllib

import pytest

from countertalk.config import BACKEND_DIR, get_settings
from scripts import check_setup
from scripts.check_setup import (
    REQUIRED_PYTHON,
    Check,
    check_database,
    check_dev_tool_keys,
    check_keys,
    check_python,
    check_tool,
    report,
)
from tests.conftest import POSTGRES_HOST, SERVER_URL, TEST_DATABASE


def test_python_312_is_ok() -> None:
    assert check_python().status == "ok"


def test_tool_that_prints_its_version_is_ok() -> None:
    check = check_tool(sys.executable, ["--version"])

    assert check.status == "ok"
    assert check.detail.startswith("Python 3.12")


def test_missing_tool_fails() -> None:
    check = check_tool("countertalk-no-such-tool", ["-version"])

    assert check.status == "fail"
    assert "not found" in check.detail


def test_tool_that_exits_with_an_error_fails() -> None:
    check = check_tool(sys.executable, ["-c", "raise SystemExit(3)"])

    assert check.status == "fail"
    assert "exit code 3" in check.detail


def test_tool_that_hangs_fails_after_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check_setup, "SETUP_CHECK_TIMEOUT_MS", 300)

    check = check_tool(sys.executable, ["-c", "import time; time.sleep(30)"])

    assert check.status == "fail"
    assert "timed out" in check.detail


def test_reachable_database_is_ok() -> None:
    check = check_database()

    assert check.status == "ok"
    assert "countertalk:countertalk@" not in check.detail


def test_unreachable_database_fails_without_showing_the_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"{SERVER_URL}/countertalk_missing")
    get_settings.cache_clear()

    check = check_database()

    assert check.status == "fail"
    assert "countertalk_missing" in check.detail
    assert "countertalk:countertalk@" not in check.detail


def test_absent_key_is_skipped_and_present_key_is_ok_without_its_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "")
    monkeypatch.setenv("SECRET_KEY", "secret-key-not-real")
    get_settings.cache_clear()

    by_name = {check.name: check for check in check_keys()}

    assert by_name["MODEL_API_KEY"].status == "skip"
    assert by_name["SECRET_KEY"].status == "ok"
    assert "secret-key-not-real" not in by_name["SECRET_KEY"].detail


def test_report_prints_one_line_per_check_and_fails_if_any_failed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    checks = [Check("ok", "python", "3.12.10"), Check("fail", "ffmpeg", "not found on PATH")]

    exit_code = report(checks)

    assert exit_code == 1
    assert capsys.readouterr().out.splitlines() == [
        "ok    python: 3.12.10",
        "fail  ffmpeg: not found on PATH",
    ]


def test_report_succeeds_when_nothing_failed(capsys: pytest.CaptureFixture[str]) -> None:
    assert report([Check("ok", "python", "3.12.10"), Check("skip", "MODEL_API_KEY", "")]) == 0


def test_bad_database_url_is_one_fail_line_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "mysql://admin:pw-not-real@db.example.com/app")
    get_settings.cache_clear()

    exit_code = check_setup.main([])

    lines = capsys.readouterr().out.splitlines()
    assert exit_code == 1
    assert all(line.split()[0] in {"ok", "skip", "fail"} for line in lines)
    assert any(line.startswith("fail  settings:") and "DATABASE_URL" in line for line in lines)
    assert "pw-not-real" not in "\n".join(lines)


def test_wrong_password_is_reported_as_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"{SERVER_URL.replace(':countertalk@', ':wrong@')}/postgres")
    get_settings.cache_clear()

    check = check_database()

    assert check.status == "fail"
    assert "password" in check.detail
    assert "wrong" not in check.detail


def test_missing_database_is_reported_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"{SERVER_URL}/countertalk_missing")
    get_settings.cache_clear()

    check = check_database()

    assert check.status == "fail"
    assert "doesn't exist" in check.detail


def test_database_url_without_a_port_shows_the_default_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"{SERVER_URL.removesuffix(':5432')}/{TEST_DATABASE}")
    get_settings.cache_clear()

    check = check_database()

    assert check.status == "ok"
    assert check.detail == f"{POSTGRES_HOST}:5432/{TEST_DATABASE}"


def test_required_python_matches_pyproject() -> None:
    project = tomllib.loads((BACKEND_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    major, minor = REQUIRED_PYTHON

    assert project["project"]["requires-python"] == f">={major}.{minor},<{major}.{minor + 1}"


def test_database_only_checks_just_the_settings_and_postgres(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = check_setup.main(["--database"])

    assert exit_code == 0
    assert capsys.readouterr().out.splitlines() == [
        "ok    settings: valid",
        f"ok    postgres: {POSTGRES_HOST}:5432/{TEST_DATABASE}",
    ]


def test_database_only_gives_the_plain_reason_without_the_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"{SERVER_URL.replace(':countertalk@', ':not-the-pw@')}/x")
    get_settings.cache_clear()

    exit_code = check_setup.main(["--database"])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "the password was refused" in out
    assert "not-the-pw" not in out


def test_database_only_cant_connect_hint_says_nothing_about_docker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # .invalid never resolves (RFC 2606), so this fails at once, like a host that's down.
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:not-the-pw@db.invalid:5432/app")
    get_settings.cache_clear()

    exit_code = check_setup.main(["--database"])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "can't connect" in out
    assert "docker" not in out.lower()


def test_the_full_check_still_suggests_docker_compose_for_local_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:not-the-pw@db.invalid:5432/app")
    get_settings.cache_clear()

    assert "docker compose up -d db" in check_database().detail


def test_database_only_reports_bad_settings_as_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "mysql://u:not-the-pw@db.example.com/app")
    get_settings.cache_clear()

    exit_code = check_setup.main(["--database"])

    lines = capsys.readouterr().out.splitlines()
    assert exit_code == 1
    assert lines == ["fail  settings: invalid in backend/.env or the environment: DATABASE_URL"]


def test_the_dev_tool_key_is_optional_and_never_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SARVAM_API_KEY", "")
    [absent] = check_dev_tool_keys()
    monkeypatch.setenv("SARVAM_API_KEY", "not-a-real-key")
    [present] = check_dev_tool_keys()

    assert (absent.status, absent.name) == ("skip", "SARVAM_API_KEY")
    assert "optional" in absent.detail
    assert present.status == "ok"
    assert "not-a-real-key" not in present.detail
