"""The visits and shops API (batch 3): only the HTTP part (CODING_GUIDELINES §3.10).

The rules themselves are tested where they live (test_visits.py). Here: each route's success,
the status and code of each error it returns, what the route itself checks before the domain
(headers, the body's length, the order of its dependencies), and the races that need HTTP.
"""

import asyncio
import hashlib
import logging
import threading
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from countertalk import auth, storage, visits
from countertalk.db import session_scope
from countertalk.limits import LOCK_TIMEOUT_MS, MAX_SEGMENT_BYTES, MAX_SEGMENTS_PER_VISIT
from countertalk.models import Org, Role, Shop, User, Visit
from tests.conftest import HTTPS, give_transcript
from tests.test_review_api import signed_in_demo
from tests.test_visits import staged_files

RACE_TIMEOUT_S = 2 * LOCK_TIMEOUT_MS / 1000


@pytest.fixture(autouse=True)
def demo(client: TestClient) -> None:
    signed_in_demo(client)


def a_shop(client: TestClient) -> dict:
    return client.get("/api/shops").json()["items"][0]


def new_visit(client: TestClient, kind: str = "conversation") -> str:
    response = client.post(
        "/api/visits", json={"shop_id": a_shop(client)["id"], "kind": kind, "client": "web"}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def put_segment(
    client: TestClient, visit_id: str, seq: int, body: bytes, sha: str | None = None
) -> httpx.Response:
    headers = {"X-Content-SHA256": sha if sha is not None else hashlib.sha256(body).hexdigest()}
    return client.put(f"/api/visits/{visit_id}/segments/{seq}", content=body, headers=headers)


def complete(client: TestClient, visit_id: str, body: dict) -> httpx.Response:
    return client.post(f"/api/visits/{visit_id}/complete", json=body)


def other_org() -> tuple[str, str]:
    """Another org with a shop and a visit: (visit_id, shop_id)."""
    with session_scope() as session:
        org = Org(name="Other Org")
        session.add(org)
        session.flush()
        rep = User(org_id=org.id, email="other-rep@example.com", name="Rep", role=Role.REP)
        shop = Shop(org_id=org.id, name="Other Shop", area="Other Area")
        session.add_all([rep, shop])
        session.flush()
        visit = visits.create_visit(session, org.id, rep.id, shop.id, "conversation", "web")
        return visit.id, shop.id


def remove_the_demo_rep() -> None:
    with session_scope() as session:
        session.execute(delete(Visit))
        session.delete(session.scalars(select(User).where(User.email == "rep@example.com")).one())


def a_do_not_record_shop(client: TestClient) -> str:
    shop_id = a_shop(client)["id"]
    with session_scope() as session:
        shop = session.get(Shop, shop_id)
        assert shop is not None
        shop.do_not_record = True
    return shop_id


# --- one success per route --------------------------------------------------------------------


def test_shops_are_listed_in_the_list_shape(client: TestClient) -> None:
    body = client.get("/api/shops").json()

    assert body["next_cursor"] is None
    assert {"id", "name", "area", "do_not_record"} == set(body["items"][0])


def test_a_created_visit_is_returned_recording_and_read_back(client: TestClient) -> None:
    shop = a_shop(client)

    response = client.post(
        "/api/visits", json={"shop_id": shop["id"], "kind": "conversation", "client": "web"}
    )

    assert response.status_code == 201
    visit = response.json()
    assert (visit["status"], visit["shop_id"], visit["completed_at"]) == (
        "recording",
        shop["id"],
        None,
    )
    assert visit["created_at"].endswith("Z")
    assert visit["processing"] is False
    assert client.get(f"/api/visits/{visit['id']}").json() == visit


def test_visits_are_listed_in_pages(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(visits, "VISITS_PAGE_SIZE", 2)
    made = [new_visit(client) for _ in range(3)]

    first = client.get("/api/visits").json()
    second = client.get("/api/visits", params={"cursor": first["next_cursor"]}).json()

    assert [v["id"] for v in first["items"] + second["items"]] == list(reversed(made))
    assert second["next_cursor"] is None


def test_a_new_segment_is_201_and_the_same_one_again_is_200(client: TestClient) -> None:
    visit_id = new_visit(client)

    first = put_segment(client, visit_id, 0, b"segment zero")
    again = put_segment(client, visit_id, 0, b"segment zero")

    assert (first.status_code, again.status_code) == (201, 200)
    assert set(first.json()) == {"seq", "bytes", "sha256", "received_at"}
    assert first.json()["received_at"] == again.json()["received_at"]
    assert storage.segment_path(visit_id, 0).read_bytes() == b"segment zero"


def test_complete_returns_the_visit_uploaded(client: TestClient) -> None:
    visit_id = new_visit(client)
    for seq in range(2):
        put_segment(client, visit_id, seq, f"part {seq}".encode())

    response = complete(client, visit_id, {"last_seq": 1, "ext": "webm"})

    assert response.status_code == 200
    assert (response.json()["status"], response.json()["processing"]) == ("uploaded", True)
    assert response.json()["completed_at"].endswith("Z")
    listed = client.get("/api/visits").json()["items"]
    assert [(v["id"], v["processing"]) for v in listed] == [(visit_id, True)]


def test_a_visits_transcript_is_returned_with_its_turns(client: TestClient) -> None:
    visit_id = new_visit(client)
    transcript_id = give_transcript(visit_id)

    response = client.get(f"/api/visits/{visit_id}/transcript")

    assert response.status_code == 200
    body = response.json()
    assert (body["id"], body["visit_id"], body["language_code"]) == (
        transcript_id,
        visit_id,
        "hi,en",
    )
    assert [(t["speaker_label"], t["text"]) for t in body["turns"]] == [
        ("c0:A", "Namaste ji."),
        ("c0:B", "Haan bolo."),
    ]
    assert set(body["turns"][0]) == {"turn_id", "speaker_label", "start_ms", "end_ms", "text"}


PLAYBACK = bytes(range(256)) * 40


def test_a_visits_audio_plays_and_seeks(client: TestClient) -> None:
    visit_id = new_visit(client)
    storage.playback_path(visit_id).parent.mkdir(parents=True)
    storage.playback_path(visit_id).write_bytes(PLAYBACK)

    whole = client.get(f"/api/visits/{visit_id}/audio")
    middle = client.get(f"/api/visits/{visit_id}/audio", headers={"Range": "bytes=4000-4999"})

    assert whole.status_code == 200
    assert (whole.headers["content-type"], whole.headers["accept-ranges"]) == ("audio/mp4", "bytes")
    assert whole.content == PLAYBACK
    assert whole.headers["cache-control"] == "private, no-cache"
    assert middle.status_code == 206
    assert middle.headers["content-range"] == f"bytes 4000-4999/{len(PLAYBACK)}"
    assert middle.content == PLAYBACK[4000:5000]


# --- each error's status and code ----------------------------------------------------------------

Request = Callable[[TestClient], httpx.Response]


def visit_extra_field(client: TestClient) -> httpx.Response:
    body = {"shop_id": a_shop(client)["id"], "kind": "notes", "client": "web", "status": "x"}
    return client.post("/api/visits", json=body)


def visit_at_another_orgs_shop(client: TestClient) -> httpx.Response:
    body = {"shop_id": other_org()[1], "kind": "conversation", "client": "web"}
    return client.post("/api/visits", json=body)


def conversation_at_a_do_not_record_shop(client: TestClient) -> httpx.Response:
    body = {"shop_id": a_do_not_record_shop(client), "kind": "conversation", "client": "web"}
    return client.post("/api/visits", json=body)


def another_orgs_visit(client: TestClient) -> httpx.Response:
    return client.get(f"/api/visits/{other_org()[0]}")


def a_malformed_cursor(client: TestClient) -> httpx.Response:
    return client.get("/api/visits", params={"cursor": "not-a-cursor"})


def without_the_demo_rep(client: TestClient) -> httpx.Response:
    remove_the_demo_rep()
    return client.get("/api/visits")


@pytest.mark.parametrize(
    ("request_", "status", "code"),
    [
        (visit_extra_field, 422, "invalid_request"),
        (visit_at_another_orgs_shop, 404, "shop_not_found"),
        (conversation_at_a_do_not_record_shop, 409, "shop_do_not_record"),
        (another_orgs_visit, 404, "visit_not_found"),
        (a_malformed_cursor, 400, "invalid_cursor"),
        (
            lambda client: client.get(f"/api/visits/{new_visit(client)}/transcript"),
            404,
            "transcript_not_found",
        ),
        (
            lambda client: client.get(f"/api/visits/{other_org()[0]}/transcript"),
            404,
            "visit_not_found",
        ),
        (
            lambda client: client.get(f"/api/visits/{new_visit(client)}/audio"),
            404,
            "audio_not_found",
        ),
        (lambda client: client.get(f"/api/visits/{other_org()[0]}/audio"), 404, "visit_not_found"),
        (without_the_demo_rep, 401, "signed_out"),  # their session went with them
    ],
)
def test_visit_and_list_errors(
    client: TestClient, request_: Request, status: int, code: str
) -> None:
    response = request_(client)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def streamed_with_no_length(client: TestClient, visit_id: str) -> httpx.Response:
    def chunks():
        yield b"streamed with no length"

    sha = hashlib.sha256(b"streamed with no length").hexdigest()
    return client.put(
        f"/api/visits/{visit_id}/segments/0", content=chunks(), headers={"X-Content-SHA256": sha}
    )


def no_checksum(client: TestClient, visit_id: str) -> httpx.Response:
    return client.put(f"/api/visits/{visit_id}/segments/0", content=b"the bytes")


def different_bytes_again(client: TestClient, visit_id: str) -> httpx.Response:
    put_segment(client, visit_id, 0, b"original")
    return put_segment(client, visit_id, 0, b"different")


def a_new_segment_after_complete(client: TestClient, visit_id: str) -> httpx.Response:
    put_segment(client, visit_id, 0, b"zero")
    complete(client, visit_id, {"last_seq": 0, "ext": "webm"})
    return put_segment(client, visit_id, 1, b"one")


def bad(sha: str) -> Callable[[TestClient, str], httpx.Response]:
    return lambda client, visit_id: put_segment(client, visit_id, 0, b"the bytes", sha=sha)


@pytest.mark.parametrize(
    ("request_", "status", "code"),
    [
        (streamed_with_no_length, 411, "length_required"),
        (no_checksum, 400, "checksum_required"),
        (bad(""), 400, "invalid_checksum"),
        (bad("ABC"), 400, "invalid_checksum"),
        (bad("A" * 64), 400, "invalid_checksum"),
        (bad(hashlib.sha256(b"other").hexdigest()), 400, "segment_checksum_mismatch"),
        (lambda client, visit_id: put_segment(client, visit_id, 0, b""), 400, "empty_segment"),
        (
            lambda client, visit_id: put_segment(client, visit_id, MAX_SEGMENTS_PER_VISIT, b"x"),
            400,
            "invalid_seq",
        ),
        (lambda client, _: put_segment(client, other_org()[0], 0, b"x"), 404, "visit_not_found"),
        (different_bytes_again, 409, "segment_conflict"),
        (a_new_segment_after_complete, 409, "visit_not_recording"),
    ],
)
def test_segment_errors_leave_no_staged_file(
    client: TestClient,
    tmp_path: Path,
    request_: Callable[[TestClient, str], httpx.Response],
    status: int,
    code: str,
) -> None:
    response = request_(client, new_visit(client))

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert staged_files(tmp_path) == []


@pytest.mark.parametrize(
    ("segments", "body", "status", "code"),
    [
        ([0, 2], {"last_seq": 2, "ext": "webm"}, 409, "segments_missing"),
        ([0, 1], {"last_seq": 0, "ext": "webm"}, 409, "unexpected_segments"),
        ([0], {"last_seq": 0, "ext": "exe"}, 400, "unsupported_extension"),
        ([0], {"last_seq": -1, "ext": "webm"}, 400, "invalid_seq"),
        # A whole number only: JSON true would otherwise count as segment 1.
        ([0], {"last_seq": True, "ext": "webm"}, 422, "invalid_request"),
        ([0], {"last_seq": 0, "ext": "webm", "extra": 1}, 422, "invalid_request"),
    ],
)
def test_complete_errors(
    client: TestClient, segments: list[int], body: dict, status: int, code: str
) -> None:
    visit_id = new_visit(client)
    for seq in segments:
        put_segment(client, visit_id, seq, f"part {seq}".encode())

    response = complete(client, visit_id, body)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code


@pytest.mark.parametrize(
    ("segments", "last_seq", "details"),
    [
        ([0, 2], 2, {"missing_seqs": [1]}),
        ([0, 1], 0, {"unexpected_seqs": [1]}),
        ([0], None, {"seq": 0}),  # the same segment again with other bytes
    ],
)
def test_a_conflict_says_which_segments_in_its_details(
    client: TestClient, segments: list[int], last_seq: int | None, details: dict
) -> None:
    visit_id = new_visit(client)
    for seq in segments:
        put_segment(client, visit_id, seq, b"first bytes")

    if last_seq is None:
        response = put_segment(client, visit_id, 0, b"other bytes")
    else:
        response = complete(client, visit_id, {"last_seq": last_seq, "ext": "webm"})

    assert response.json()["error"]["details"] == details


# --- what the route checks before the body is read (raw ASGI) ------------------------------------
# An HTTP client won't send a Content-Length that disagrees with its body, or break off.


def raw_put(
    app: FastAPI,
    path: str,
    headers: dict[str, str],
    chunks: list[bytes],
    disconnect_after: int | None = None,
) -> tuple[int, bytes, int]:
    """Sends the request as a server would. Header values go as latin-1, as on the wire."""

    async def call() -> tuple[int, bytes, int]:
        sent: list[dict] = []
        pending = list(chunks)
        done = asyncio.Event()
        read_count = 0

        async def receive() -> dict:
            nonlocal read_count
            if disconnect_after is not None and read_count >= disconnect_after:
                return {"type": "http.disconnect"}
            if pending:
                read_count += 1
                chunk = pending.pop(0)
                return {"type": "http.request", "body": chunk, "more_body": bool(pending)}
            await done.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            sent.append(message)
            if message["type"] == "http.response.body" and not message.get("more_body"):
                done.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "PUT",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [(k.lower().encode(), v.encode("latin-1")) for k, v in headers.items()],
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
        }
        await app(scope, receive, send)
        start = next(m for m in sent if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return start["status"], body, read_count

    return asyncio.run(call())


def signed(client: TestClient, headers: dict[str, str]) -> dict[str, str]:
    """The headers with the signed-in client's session cookie (batch 16)."""
    return {**headers, "cookie": f"{auth.COOKIE}={client.cookies[auth.COOKIE]}"}


def own_client(app: FastAPI, client: TestClient) -> TestClient:
    """Another client of the app, signed in as `client` is: for races across clients."""
    cookies = {auth.COOKIE: client.cookies[auth.COOKIE]}
    return TestClient(app, base_url=HTTPS, cookies=cookies, raise_server_exceptions=False)


BODY = b"the bytes"
GOOD = {"content-length": str(len(BODY)), "x-content-sha256": hashlib.sha256(BODY).hexdigest()}
MIB = b"x" * (1024 * 1024)


@pytest.mark.parametrize(
    ("visit", "headers", "chunks", "status", "code", "read_nothing"),
    [
        # Unicode digits pass str.isdigit() but not int(): a 400, not a 500.
        ("ok", {**GOOD, "content-length": "²"}, [BODY], 400, "invalid_content_length", True),
        (
            "ok",
            {**GOOD, "content-length": str(MAX_SEGMENT_BYTES + 1)},
            [BODY],
            413,
            "segment_too_large",
            True,
        ),
        (
            "ok",
            {**GOOD, "content-length": "10"},
            [MIB] * (MAX_SEGMENT_BYTES // len(MIB) + 1),
            413,
            "segment_too_large",
            False,
        ),
        ("malformed", GOOD, [BODY], 422, "invalid_request", True),
        ("no_user", GOOD, [BODY], 401, "signed_out", True),
        ("bad_seq", GOOD, [BODY], 400, "invalid_seq", True),
    ],
    ids=["unicode-length", "length-over", "body-over", "malformed-id", "no-user", "bad-seq"],
)
def test_the_route_refuses_before_reading_or_beyond_the_limit(
    app: FastAPI,
    client: TestClient,
    tmp_path: Path,
    visit: str,
    headers: dict[str, str],
    chunks: list[bytes],
    status: int,
    code: str,
    read_nothing: bool,
) -> None:
    visit_id = "not-a-uuid" if visit == "malformed" else new_visit(client)
    if visit == "no_user":
        remove_the_demo_rep()

    seq = MAX_SEGMENTS_PER_VISIT if visit == "bad_seq" else 0
    path = f"/api/visits/{visit_id}/segments/{seq}"
    got, body, chunks_read = raw_put(app, path, signed(client, headers), chunks)

    assert got == status
    assert code.encode() in body
    assert (chunks_read == 0) is read_nothing
    assert staged_files(tmp_path) == []


def test_an_upload_that_breaks_off_is_a_400_and_not_an_error_in_the_log(
    app: FastAPI, client: TestClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    visit_id = new_visit(client)
    headers = {"content-length": "2048", "x-content-sha256": "0" * 64}

    with caplog.at_level(logging.INFO):
        status, body, _ = raw_put(
            app,
            f"/api/visits/{visit_id}/segments/0",
            signed(client, headers),
            [b"x" * 1024, b"x" * 1024],
            1,
        )

    assert status == 400
    assert b"upload_interrupted" in body
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert staged_files(tmp_path) == []


# --- races that need HTTP ----------------------------------------------------------------------


def test_two_uploads_of_one_segment_with_different_bytes_store_one(
    app: FastAPI, client: TestClient
) -> None:
    visit_id = new_visit(client)
    start = threading.Barrier(2, timeout=RACE_TIMEOUT_S)
    statuses: list[int] = []

    def send(body: bytes) -> None:
        with own_client(app, client) as own:
            start.wait()
            statuses.append(put_segment(own, visit_id, 0, body).status_code)

    threads = [threading.Thread(target=send, args=(body,)) for body in (b"one", b"two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=RACE_TIMEOUT_S)

    assert sorted(statuses) == [201, 409]


def test_while_a_segment_is_staged_the_visit_is_not_locked(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    visit_id = new_visit(client)
    staging, release = threading.Event(), threading.Event()
    real_stage = storage.stage

    def stage_slowly(body: bytes) -> Path:
        staging.set()
        release.wait(RACE_TIMEOUT_S)
        return real_stage(body)

    monkeypatch.setattr(storage, "stage", stage_slowly)
    statuses: list[int] = []

    def upload() -> None:
        with own_client(app, client) as own:
            statuses.append(put_segment(own, visit_id, 0, b"the bytes").status_code)

    thread = threading.Thread(target=upload)
    thread.start()
    assert staging.wait(RACE_TIMEOUT_S)
    try:
        # NOWAIT fails at once if anyone holds the row; nobody may while bytes are written.
        with session_scope() as other:
            other.scalars(
                select(Visit).where(Visit.id == visit_id).with_for_update(nowait=True)
            ).one()
    finally:
        release.set()
        thread.join(RACE_TIMEOUT_S)

    assert statuses == [201]
