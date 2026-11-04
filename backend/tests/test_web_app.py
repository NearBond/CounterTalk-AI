"""Serving the built web app from FastAPI (batch 2, decision 6), and the landing page at "/"
for visitors who aren't signed in (batch F6)."""

import asyncio
import os
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from countertalk.auth import COOKIE
from countertalk.config import get_settings
from countertalk.main import create_app

INDEX = "<!doctype html><title>CounterTalk</title><div id=root></div>"
LANDING = "<!doctype html><title>CounterTalk</title><main id=main></main>"
STYLES = b"body { margin: 0; }\n"
# Any value: only the cookie's presence is checked, so a stale one counts too.
SIGNED_IN = {"Cookie": f"{COOKIE}=stale"}
CANARY = "canary text that must never be served"


@pytest.fixture
def dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A built app at <tmp>/web/dist, laid out like the repo, with secrets around and inside it."""
    built = tmp_path / "web" / "dist"
    (built / "assets").mkdir(parents=True)
    (built / "index.html").write_text(INDEX, encoding="utf-8")
    (built / "assets" / "app-3f2a1b.js").write_bytes(b"export {};\n")
    (built / "landing" / "css").mkdir(parents=True)
    (built / "landing" / "index.html").write_text(LANDING, encoding="utf-8")
    (built / "landing" / "css" / "styles.css").write_bytes(STYLES)
    (built / ".env").write_text(CANARY, encoding="utf-8")
    (built / "assets" / ".hidden").write_text(CANARY, encoding="utf-8")
    for outside in (tmp_path / "backend" / ".env", tmp_path / "web" / "backend" / ".env"):
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text(CANARY, encoding="utf-8")
    (tmp_path / "outside.txt").write_text(CANARY, encoding="utf-8")
    monkeypatch.setenv("WEB_DIST_DIR", str(built))
    get_settings.cache_clear()
    return built


@pytest.fixture
def web_app(dist: Path) -> FastAPI:
    return create_app()


@pytest.fixture
def web_client(web_app: FastAPI) -> TestClient:
    return TestClient(web_app, raise_server_exceptions=False)


def raw_get(app: FastAPI, path: str, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
    """Sends the path exactly as written, the way a server like uvicorn passes it on.

    An HTTP client would tidy `/../` away before sending, so traversal needs the raw ASGI call.
    """

    async def call() -> tuple[int, dict[str, str], bytes]:
        messages: list[dict] = []
        request_sent = False
        response_done = asyncio.Event()

        async def receive() -> dict:
            # Like a server: the request once, then a disconnect once the response is out.
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await response_done.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            messages.append(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                response_done.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": unquote(path),
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
        }
        await app(scope, receive, send)
        start = next(m for m in messages if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
        return start["status"], headers, body

    return asyncio.run(call())


def test_root_serves_the_landing_page_without_the_session_cookie(web_client: TestClient) -> None:
    response = web_client.get("/")

    assert response.status_code == 200
    assert response.text == LANDING
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["vary"] == "Cookie"


def test_root_serves_the_app_with_the_session_cookie(web_client: TestClient) -> None:
    response = web_client.get("/", headers=SIGNED_IN)

    assert response.status_code == 200
    assert response.text == INDEX
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["vary"] == "Cookie"


def test_the_landing_page_s_files_are_served_and_never_kept_for_good(
    web_client: TestClient,
) -> None:
    # Not content-hashed like Vite's assets/, so a browser always asks again.
    response = web_client.get("/landing/css/styles.css")

    assert response.status_code == 200
    assert response.content == STYLES
    assert response.headers["content-type"].startswith("text/css")
    assert response.headers["cache-control"] == "no-cache"


def test_a_build_without_the_landing_page_serves_the_app_at_the_root(
    dist: Path, web_client: TestClient
) -> None:
    (dist / "landing" / "index.html").unlink()

    assert web_client.get("/").text == INDEX


def test_a_built_asset_is_served_with_its_type_and_cached_for_good(web_client: TestClient) -> None:
    response = web_client.get("/assets/app-3f2a1b.js")

    assert response.status_code == 200
    assert response.content == b"export {};\n"
    assert "javascript" in response.headers["content-type"]
    assert "immutable" in response.headers["cache-control"]


@pytest.mark.parametrize("path", ["/visits", "/visits/3f2a1b/review"])
def test_any_other_page_gets_the_app(web_client: TestClient, path: str) -> None:
    response = web_client.get(path)

    assert response.status_code == 200
    assert response.text == INDEX


def test_a_missing_asset_is_a_404_not_the_app(web_client: TestClient) -> None:
    # A stale tab asking for an old hashed file must get an error it can recognise, not HTML.
    response = web_client.get("/assets/app-old.js")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_a_new_build_is_picked_up_without_a_restart(dist: Path, web_client: TestClient) -> None:
    assert web_client.get("/assets/app-3f2a1b.js").status_code == 200
    (dist / "assets" / "app-3f2a1b.js").unlink()
    (dist / "assets" / "app-9e8d7c.js").write_bytes(b"export const v = 2;\n")
    index = dist / "index.html"
    index.write_text(INDEX + "<!-- build 2 -->", encoding="utf-8")
    later = index.stat().st_mtime + 5
    os.utime(index, (later, later))

    assert web_client.get("/assets/app-9e8d7c.js").content == b"export const v = 2;\n"
    assert web_client.get("/assets/app-3f2a1b.js").status_code == 404
    assert web_client.get("/visits").text.endswith("<!-- build 2 -->")


def test_head_sends_the_headers_only(web_client: TestClient) -> None:
    response = web_client.head("/")

    assert response.status_code == 200
    assert response.content == b""


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/api"), ("GET", "/api/no-such-thing"), ("POST", "/api/no-such-thing")],
)
def test_an_unknown_api_path_is_the_json_404(
    web_client: TestClient, method: str, path: str
) -> None:
    response = web_client.request(method, path)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_the_api_still_answers_with_the_app_mounted(web_client: TestClient) -> None:
    assert web_client.get("/api/health").json() == {"status": "ok", "database": "ok"}
    assert web_client.delete("/api/health").json()["error"]["code"] == "method_not_allowed"


def test_the_session_cookie_changes_nothing_under_api(web_client: TestClient) -> None:
    assert web_client.get("/api/health", headers=SIGNED_IN).json()["status"] == "ok"
    missing = web_client.get("/api/no-such-thing", headers=SIGNED_IN)
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "not_found")


@pytest.mark.parametrize(
    "path",
    [
        "/.env",
        "/../backend/.env",
        "/%2e%2e/backend/.env",
        "/assets/../../backend/.env",
        "/..%2f..%2fbackend/.env",
        "/assets%5c..%5c..%5coutside.txt",
        "/../outside.txt",
        "/assets/.hidden",
        "/./.env",
    ],
)
def test_a_dotfile_or_a_path_out_of_dist_never_returns_a_file(web_app: FastAPI, path: str) -> None:
    status, _headers, body = raw_get(web_app, path)

    assert CANARY.encode() not in body
    # The app shell, or the JSON 404 for anything under /assets/; never a file.
    if path.startswith("/assets/"):
        assert (status, b'"not_found"' in body) == (404, True)
    else:
        assert (status, body) == (200, INDEX.encode())


def test_a_symlink_out_of_dist_is_not_served(dist: Path, tmp_path: Path) -> None:
    try:
        os.symlink(tmp_path / "outside.txt", dist / "assets" / "linked.txt")
    except OSError:
        pytest.skip("this machine can't create symlinks")

    status, _headers, body = raw_get(create_app(), "/assets/linked.txt")

    assert CANARY.encode() not in body
    assert status == 404


def test_without_a_built_app_nothing_is_served_and_the_api_still_works(
    client: TestClient,
) -> None:
    assert client.get("/").status_code == 404
    assert client.get("/").json()["error"]["code"] == "not_found"
    assert client.get("/api/health").status_code == 200


def test_a_folder_without_index_html_is_not_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "notes.txt").write_text(CANARY, encoding="utf-8")
    monkeypatch.setenv("WEB_DIST_DIR", str(tmp_path / "dist"))
    get_settings.cache_clear()

    response = TestClient(create_app()).get("/notes.txt")

    assert response.status_code == 404
    assert CANARY not in response.text
