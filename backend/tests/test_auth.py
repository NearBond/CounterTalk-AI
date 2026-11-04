"""Sign-in (batch 16): passwords, sessions, the wait after wrong passwords, and the app's key.

Every route but sign-in, sign-out and health needs a session; a refusal says the same whether
or not the email has an account; the wait holds no thread; nothing secret reaches the logs."""

import asyncio
import hashlib
import logging
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import anyio
import httpx
import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from countertalk import auth, limits
from countertalk.config import BACKEND_DIR, Misconfigured, get_settings
from countertalk.db import session_scope
from countertalk.main import create_app
from countertalk.models import SigninFailure, User, UserSession, utc_now
from tests.conftest import HTTPS, sign_in
from tests.test_visits import Org_, make_org

PASSWORD = "correct horse battery staple"
WAIT_TIMEOUT_S = 10
# A stand-in for MODEL_API_KEY, word-like so no secret scanner takes it for one.
CANARY_KEY = "canary-canary-canary"


@pytest.fixture
def org() -> Org_:
    return make_org()


def email_of(org: Org_) -> str:
    with session_scope(read_only=True) as session:
        return session.get_one(User, org.rep_id).email


@pytest.fixture
def rep(org: Org_) -> str:
    """The org's rep, with PASSWORD: their email."""
    email = email_of(org)
    with session_scope() as session:
        assert auth.set_password(session, email, PASSWORD)
    return email


def signing_in(client: TestClient, email: str, password: str) -> httpx.Response:
    return client.post("/api/auth/signin", json={"email": email, "password": password})


def failures_of(email: str) -> int:
    with session_scope(read_only=True) as session:
        found = session.get(SigninFailure, email)
        return found.failures if found else 0


def sessions() -> int:
    with session_scope(read_only=True) as session:
        return session.scalar(select(func.count()).select_from(UserSession)) or 0


# --- signing in ------------------------------------------------------------------------------


def test_the_right_password_signs_in_with_a_secure_cookie(
    client: TestClient, org: Org_, rep: str
) -> None:
    response = signing_in(client, rep.upper(), PASSWORD)  # an email in any case

    assert response.status_code == 200
    assert response.json() == {
        "id": org.rep_id,
        "name": "Rep",
        "email": rep,
        "role": "rep",
        "team": None,
        "org_name": "Visit Test Org",
        "ai_key_set": False,  # the tests pin MODEL_API_KEY empty
    }
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{auth.COOKIE}=")
    for flag in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"):
        assert f"; {flag}" in cookie
    assert f"; Max-Age={limits.SESSION_DAYS * 24 * 60 * 60}" in cookie
    assert client.get("/api/auth/me").json()["id"] == org.rep_id


def test_who_is_signed_in_says_whether_the_model_key_is_set_never_the_key(
    client: TestClient, rep: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Batch F8: the app's warning bar. Read on every call, so a restart with a key clears it.
    assert signing_in(client, rep, PASSWORD).json()["ai_key_set"] is False
    monkeypatch.setenv("MODEL_API_KEY", CANARY_KEY)
    get_settings.cache_clear()

    response = client.get("/api/auth/me")

    assert response.json()["ai_key_set"] is True
    assert CANARY_KEY not in response.text
    assert CANARY_KEY not in signing_in(client, rep, PASSWORD).text


def test_only_the_token_s_hash_is_stored(client: TestClient, rep: str) -> None:
    signing_in(client, rep, PASSWORD)
    token = auth.token_in(client.cookies[auth.COOKIE])

    with session_scope(read_only=True) as session:
        [stored] = session.scalars(select(UserSession)).all()
    assert token is not None
    assert stored.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert stored.expires_at - stored.created_at == timedelta(days=limits.SESSION_DAYS)


def no_such_email(client: TestClient, rep: str) -> httpx.Response:
    return signing_in(client, "nobody@example.com", PASSWORD)


def wrong_password(client: TestClient, rep: str) -> httpx.Response:
    return signing_in(client, rep, "not the password at all")


def no_password_set(client: TestClient, rep: str) -> httpx.Response:
    with session_scope() as session:
        session.execute(update(User).where(User.email == rep).values(password_hash=None))
    return signing_in(client, rep, PASSWORD)


@pytest.mark.parametrize("attempt", [no_such_email, wrong_password, no_password_set])
def test_every_refusal_says_the_same(client: TestClient, rep: str, attempt) -> None:
    response = attempt(client, rep)

    assert response.status_code == 401
    assert response.json() == {
        "error": {"code": "wrong_email_or_password", "message": "Wrong email or password"}
    }
    assert "set-cookie" not in response.headers
    assert sessions() == 0


class Recording:
    """argon2's hasher, noting each hash it checks a password against."""

    def __init__(self, real: PasswordHasher) -> None:
        self.real = real
        self.checked: list[str] = []

    def hash(self, password: str) -> str:
        return self.real.hash(password)

    def verify(self, stored: str, password: str) -> bool:
        self.checked.append(stored)
        return self.real.verify(stored, password)


def test_an_unknown_email_is_checked_against_a_hash_too(
    client: TestClient, rep: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    hasher = Recording(auth._hasher)
    monkeypatch.setattr(auth, "_hasher", hasher)

    signing_in(client, "nobody@example.com", PASSWORD)

    assert hasher.checked == [auth._dummy_hash()]  # as long as a wrong password takes


# --- the pause after wrong passwords ----------------------------------------------------------


def back(email: str, seconds: float) -> None:
    """The email's last check, that many seconds earlier: its pause has run that long more."""
    with session_scope() as session:
        found = session.get_one(SigninFailure, email)
        found.last_checked_at -= timedelta(seconds=seconds)


def test_inside_the_pause_an_attempt_is_refused_at_once_and_not_counted(
    client: TestClient, rep: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for _ in range(limits.SIGNIN_FREE_FAILURES + 1):
        assert wrong_password(client, rep).status_code == 401
    hasher = Recording(auth._hasher)
    monkeypatch.setattr(auth, "_hasher", hasher)

    refused = signing_in(client, rep, PASSWORD)  # the 6th, the right one, within 1 s

    assert refused.status_code == 429
    assert refused.headers["retry-after"] == "1"
    assert refused.json() == {
        "error": {
            "code": "too_many_tries",
            "message": "Too many tries for this email. Try again in 1 second.",
        }
    }
    assert hasher.checked == []  # no password checked
    assert failures_of(rep) == limits.SIGNIN_FREE_FAILURES + 1  # and not counted


def test_after_the_pause_the_right_password_works_and_resets_the_count(
    client: TestClient, rep: str
) -> None:
    for _ in range(limits.SIGNIN_FREE_FAILURES + 2):
        wrong_password(client, rep)
        back(rep, limits.SIGNIN_MAX_WAIT_S)  # past each pause, so each is checked
    assert failures_of(rep) == limits.SIGNIN_FREE_FAILURES + 2

    # 6 failures: 2 s from the last check. At 1 s, still refused; past 2 s, checked.
    back(rep, -limits.SIGNIN_MAX_WAIT_S + 1)
    assert signing_in(client, rep, PASSWORD).status_code == 429
    back(rep, 1)

    assert signing_in(client, rep, PASSWORD).status_code == 200
    assert failures_of(rep) == 0


def test_the_pause_grows_a_second_a_failure_to_its_cap() -> None:
    assert [auth.wait_for(n) for n in (0, 4, 5, 13, 14, 15, 100)] == [0, 0, 1, 9, 10, 10, 10]


def test_an_unknown_email_is_paused_the_same(client: TestClient, rep: str) -> None:
    for _ in range(limits.SIGNIN_FREE_FAILURES + 1):
        no_such_email(client, rep)

    refused = no_such_email(client, rep)

    assert (refused.status_code, refused.headers["retry-after"]) == (429, "1")
    assert failures_of("nobody@example.com") == limits.SIGNIN_FREE_FAILURES + 1


def test_two_attempts_at_once_in_one_window_get_one_check(rep: str) -> None:
    for _ in range(limits.SIGNIN_FREE_FAILURES + 1):
        auth._note_failure(rep)
    back(rep, limits.SIGNIN_MAX_WAIT_S)  # the window is open: one check may go

    async def both() -> list[BaseException | tuple[str, str, str]]:
        with anyio.fail_after(WAIT_TIMEOUT_S):
            tries = [auth.sign_in(rep, "not the password at all") for _ in range(2)]
            return await asyncio.gather(*tries, return_exceptions=True)

    answers = anyio.run(both)

    assert sorted(type(a).__name__ for a in answers) == ["SignedOut", "TooMany"]
    assert failures_of(rep) == limits.SIGNIN_FREE_FAILURES + 2  # the one check counted


def test_two_wrong_passwords_at_once_both_count(rep: str) -> None:
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: auth._note_failure(rep), range(2)))

    assert failures_of(rep) == 2


def test_a_count_untouched_for_a_day_starts_again(client: TestClient, rep: str) -> None:
    for _ in range(limits.SIGNIN_FREE_FAILURES + 1):
        wrong_password(client, rep)
    back(rep, limits.SIGNIN_FAILURES_KEPT_H * 3600 + 1)

    assert wrong_password(client, rep).status_code == 401  # checked, not paused

    assert failures_of(rep) == 1


def test_housekeeping_ends_expired_sessions_and_forgets_old_counts(
    client: TestClient, org: Org_, rep: str
) -> None:
    sign_in(client, org.rep_id)
    sign_in(client, org.rep_id)  # a second session, expired below
    wrong_password(client, rep)
    auth._note_failure("other@example.com")
    with session_scope() as session:
        [expired, _] = session.scalars(select(UserSession).order_by(UserSession.created_at))
        expired.created_at -= timedelta(days=limits.SESSION_DAYS + 1)
        expired.expires_at = utc_now() - timedelta(seconds=1)
    back(rep, limits.SIGNIN_FAILURES_KEPT_H * 3600 + 1)

    with session_scope() as session:
        pruned = auth.prune(session)

    assert pruned == {"sessions_ended": 1, "signin_failures_forgotten": 1}
    assert sessions() == 1
    assert failures_of("other@example.com") == 1


def test_no_more_than_argon2_at_once_checks_run_together(
    org: Org_, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(limits, "ARGON2_AT_ONCE", 1)
    running, most = [0], [0]
    lock = threading.Lock()

    def slow(stored: str | None, password: str) -> bool:
        with lock:
            running[0] += 1
            most[0] = max(most[0], running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1
        return False

    monkeypatch.setattr(auth, "password_matches", slow)

    async def three() -> list[BaseException | tuple[str, str, str]]:
        with anyio.fail_after(WAIT_TIMEOUT_S):
            tries = [auth.sign_in(f"n{i}@example.com", "x") for i in range(3)]
            return await asyncio.gather(*tries, return_exceptions=True)

    assert [type(a).__name__ for a in anyio.run(three)] == ["SignedOut"] * 3
    assert most == [1]


# --- sessions --------------------------------------------------------------------------------


def test_without_a_session_it_s_401(client: TestClient) -> None:
    response = client.get("/api/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "signed_out"


def test_a_cookie_with_a_character_past_ascii_is_no_token() -> None:
    assert auth.token_in("abc.\u00e9") is None


@pytest.mark.parametrize(
    "cookie",
    [
        "not-a-session",
        "token.0000",  # signed by nobody
        "SIGNED",  # signed by us, for no session (made in the test, once the key is pinned)
    ],
)
def test_a_cookie_that_isn_t_a_live_session_is_401(client: TestClient, cookie: str) -> None:
    if cookie == "SIGNED":
        cookie = auth.cookie_for("a-token-no-session-has")
    client.cookies.set(auth.COOKIE, cookie)

    assert client.get("/api/auth/me").status_code == 401


def test_a_cookie_signed_with_another_key_is_401(
    client: TestClient, org: Org_, monkeypatch: pytest.MonkeyPatch
) -> None:
    sign_in(client, org.rep_id)
    monkeypatch.setenv("SECRET_KEY", "another-key")
    get_settings.cache_clear()

    assert client.get("/api/auth/me").status_code == 401


def test_an_expired_session_is_401(client: TestClient, org: Org_) -> None:
    sign_in(client, org.rep_id)
    with session_scope() as session:
        session.execute(
            update(UserSession).values(
                created_at=utc_now() - timedelta(days=15), expires_at=utc_now() - timedelta(days=1)
            )
        )

    assert client.get("/api/auth/me").status_code == 401


def test_signing_out_ends_the_session(client: TestClient, org: Org_) -> None:
    sign_in(client, org.rep_id)

    response = client.post("/api/auth/signout")

    assert response.status_code == 204
    assert f'{auth.COOKIE}=""' in response.headers["set-cookie"]
    assert sessions() == 0
    assert client.get("/api/auth/me").status_code == 401


def test_signing_out_when_signed_out_is_204(client: TestClient) -> None:
    assert client.post("/api/auth/signout").status_code == 204


def test_reading_who_is_signed_in_writes_nothing(client: TestClient, org: Org_) -> None:
    sign_in(client, org.rep_id)
    with session_scope(read_only=True) as session:
        before = session.scalars(select(UserSession.expires_at)).all()

    client.get("/api/auth/me")

    with session_scope(read_only=True) as session:
        assert session.scalars(select(UserSession.expires_at)).all() == before  # no "last seen"


PUBLIC = {
    ("POST", "/api/auth/signin"),
    ("POST", "/api/auth/signout"),
    ("GET", "/api/health"),
    # A shop's consent page: the token is the key (batch 17).
    ("GET", "/api/consent/{token}"),
    ("POST", "/api/consent/{token}"),
}
SOME_ID = "00000000-0000-4000-8000-000000000000"


def api_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Every API route, (method, path template), from the app's OpenAPI paths: they list the
    routes of included routers, which FastAPI nests out of app.routes' reach."""
    return sorted(
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    )


def test_the_route_list_sees_included_routers() -> None:
    found = api_routes(create_app())

    assert ("GET", "/api/shops") in found
    assert ("PUT", "/api/visits/{visit_id}/segments/{seq}") in found
    assert len(found) >= 30


def test_every_other_route_needs_a_session(client: TestClient) -> None:
    """The guard for routes still to come: one without the session dependency fails here."""
    open_routes = []
    for method, template in api_routes(create_app()):
        if (method, template) in PUBLIC:
            continue
        path = re.sub(r"\{[^}]+\}", SOME_ID, template)
        response = client.request(method, path)
        if response.status_code != 401:
            open_routes.append(f"{method} {template} {response.status_code}")

    assert open_routes == []


# --- the key, the password script and the logs -----------------------------------------------


def test_with_debug_off_the_app_won_t_start_without_a_secret_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECRET_KEY", "")
    get_settings.cache_clear()

    with pytest.raises(Misconfigured) as error, TestClient(create_app()):
        pass

    assert str(error.value).startswith("SECRET_KEY is empty: sign-in sessions are signed with it.")


def test_with_debug_off_nothing_is_signed_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", "")
    get_settings.cache_clear()

    with pytest.raises(Misconfigured):
        auth.cookie_for("a-token")


def test_with_debug_on_it_starts_and_signs_with_a_key_of_its_own(
    monkeypatch: pytest.MonkeyPatch, org: Org_
) -> None:
    monkeypatch.setenv("SECRET_KEY", "")
    monkeypatch.setenv("DEBUG", "1")
    get_settings.cache_clear()

    with TestClient(create_app(), base_url=HTTPS) as client:
        sign_in(client, org.rep_id)
        assert client.get("/api/auth/me").status_code == 200


def set_password(email: str, password: str | None) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key != "NEW_PASSWORD"}
    if password is not None:
        env["NEW_PASSWORD"] = password
    return subprocess.run(
        [sys.executable, str(BACKEND_DIR / "scripts" / "set_password.py"), email],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_set_password_takes_it_from_the_environment_and_ends_sessions(
    client: TestClient, org: Org_
) -> None:
    sign_in(client, org.rep_id)
    email = email_of(org)

    done = set_password(email.upper(), PASSWORD)

    assert (done.returncode, done.stderr) == (0, "")
    assert done.stdout == f"password set for {email}; their sessions are ended\n"
    assert PASSWORD not in done.stdout
    assert sessions() == 0
    assert signing_in(client, email, PASSWORD).status_code == 200


@pytest.mark.parametrize(
    ("password", "said"),
    [
        (None, "no password: set NEW_PASSWORD for this command\n"),
        ("short", "password not set: password_too_short: A password needs 12 characters or more\n"),
    ],
)
def test_set_password_refuses_in_one_line(org: Org_, password: str | None, said: str) -> None:
    done = set_password(email_of(org), password)

    assert (done.returncode, done.stdout, done.stderr) == (1, "", said)


def test_sign_in_logs_no_email_password_or_token(
    client: TestClient, rep: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        wrong_password(client, rep)
        signing_in(client, rep, PASSWORD)

    token = auth.token_in(client.cookies[auth.COOKIE]) or "no token"
    logged = caplog.text
    for secret in (rep, rep.split("@")[0], PASSWORD, token, auth.token_hash(token)):
        assert secret not in logged
    assert "signed in user=" in logged


def test_another_site_gets_no_cors_answer(client: TestClient) -> None:
    """No CORS (batch 16): the API serves the web app from its own address, so another site's
    page can't read its answers, or send a request with the session cookie that it can read."""
    asked = client.options(
        "/api/auth/me",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    read = client.get("/api/health", headers={"Origin": "https://evil.example"})

    for response in (asked, read):
        assert "access-control-allow-origin" not in response.headers
        assert "access-control-allow-credentials" not in response.headers
