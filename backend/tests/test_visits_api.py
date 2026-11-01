"""The visits and shops API (batch 3): /api/visits, its segments and complete, and /api/shops."""

import asyncio
import hashlib
import logging
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from countertalk import storage, visits
from countertalk.db import session_scope
from countertalk.limits import LOCK_TIMEOUT_MS, MAX_SEGMENT_BYTES, MAX_SEGMENTS_PER_VISIT
from countertalk.models import Org, Role, Shop, User, Visit
from scripts.seed_dev import seed

RACE_TIMEOUT_S = 2 * LOCK_TIMEOUT_MS / 1000


@pytest.fixture(autouse=True)
def demo() -> None:
    with session_scope() as session:
        seed(session)


def a_shop(client: TestClient) -> dict:
    return client.get("/api/shops").json()["items"][0]


def new_visit(client: TestClient, kind: str = "conversation") -> str:
    response = client.post(
        "/api/visits", json={"shop_id": a_shop(client)["id"], "kind": kind, "client": "web"}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def put_segment(client: TestClient, visit_id: str, seq: int, body: bytes, sha: str | None = None):
    headers = {"X-Content-SHA256": sha if sha is not None else hashlib.sha256(body).hexdigest()}
    return client.put(f"/api/visits/{visit_id}/segments/{seq}", content=body, headers=headers)


def staged_files(data_dir: Path) -> list[Path]:
    staging = data_dir / "tmp"
    return sorted(staging.iterdir()) if staging.exists() else []


def other_orgs_visit() -> str:
    return other_org()[0]


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


# --- shops and visits -------------------------------------------------------------------------


def test_shops_are_listed_in_the_list_shape(client: TestClient) -> None:
    body = client.get("/api/shops").json()

    assert body["next_cursor"] is None
    assert {"id", "name", "area", "do_not_record"} == set(body["items"][0])
    names = [shop["name"] for shop in body["items"]]
    assert names == sorted(names)


def test_creating_a_visit_returns_it_recording(client: TestClient) -> None:
    shop = a_shop(client)

    response = client.post(
        "/api/visits", json={"shop_id": shop["id"], "kind": "conversation", "client": "web"}
    )

    assert response.status_code == 201
    visit = response.json()
    assert (visit["status"], visit["kind"], visit["client"], visit["shop_id"]) == (
        "recording",
        "conversation",
        "web",
        shop["id"],
    )
    assert visit["created_at"].endswith("Z")
    assert visit["completed_at"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "conversation", "client": "web"},
        {"shop_id": "not-a-uuid", "kind": "conversation", "client": "web"},
        {"shop_id": "SHOP", "kind": "meeting", "client": "web"},
        {"shop_id": "SHOP", "kind": "conversation", "client": "desktop"},
        {"shop_id": "SHOP", "kind": "conversation", "client": "web", "status": "uploaded"},
    ],
    ids=["missing", "malformed-id", "bad-kind", "bad-client", "extra-field"],
)
def test_a_visit_of_the_wrong_shape_is_refused(client: TestClient, body: dict) -> None:
    body = {
        key: (a_shop(client)["id"] if value == "SHOP" else value) for key, value in body.items()
    }

    response = client.post("/api/visits", json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_a_visit_at_another_orgs_shop_is_not_found(client: TestClient) -> None:
    _visit_id, other_shop = other_org()

    response = client.post(
        "/api/visits", json={"shop_id": other_shop, "kind": "conversation", "client": "web"}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "shop_not_found"


def test_a_do_not_record_shop_refuses_a_conversation(client: TestClient) -> None:
    shop_id = a_shop(client)["id"]
    with session_scope() as session:
        shop = session.get(Shop, shop_id)
        assert shop is not None
        shop.do_not_record = True

    conversation = client.post(
        "/api/visits", json={"shop_id": shop_id, "kind": "conversation", "client": "web"}
    )
    notes = client.post("/api/visits", json={"shop_id": shop_id, "kind": "notes", "client": "web"})

    assert conversation.status_code == 409
    assert conversation.json()["error"]["code"] == "shop_do_not_record"
    assert notes.status_code == 201


def test_a_visit_is_read_back_and_another_orgs_is_not_found(client: TestClient) -> None:
    visit_id = new_visit(client)

    assert client.get(f"/api/visits/{visit_id}").json()["id"] == visit_id
    response = client.get(f"/api/visits/{other_orgs_visit()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "visit_not_found"


def test_visits_are_listed_in_pages(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(visits, "VISITS_PAGE_SIZE", 2)
    made = [new_visit(client) for _ in range(3)]

    first = client.get("/api/visits").json()
    second = client.get("/api/visits", params={"cursor": first["next_cursor"]}).json()

    assert [v["id"] for v in first["items"] + second["items"]] == list(reversed(made))
    assert second["next_cursor"] is None


def test_a_malformed_cursor_is_a_400(client: TestClient) -> None:
    response = client.get("/api/visits", params={"cursor": "not-a-cursor"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_cursor"


# --- segments ---------------------------------------------------------------------------------


def test_a_new_segment_is_201_and_the_same_one_again_is_200(client: TestClient) -> None:
    visit_id = new_visit(client)

    first = put_segment(client, visit_id, 0, b"segment zero")
    again = put_segment(client, visit_id, 0, b"segment zero")

    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json()["sha256"] == hashlib.sha256(b"segment zero").hexdigest()
    assert first.json()["received_at"] == again.json()["received_at"]
    assert storage.segment_path(visit_id, 0).read_bytes() == b"segment zero"


def test_different_bytes_for_a_stored_segment_are_a_409(client: TestClient) -> None:
    visit_id = new_visit(client)
    put_segment(client, visit_id, 0, b"original")

    response = put_segment(client, visit_id, 0, b"different")

    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "segment_conflict",
        "message": "Segment 0 was already uploaded with different bytes",
        "details": {"seq": 0},
    }


@pytest.mark.parametrize(
    ("sha", "code"),
    [
        ("", "invalid_checksum"),
        ("ABC", "invalid_checksum"),
        ("A" * 64, "invalid_checksum"),
        (hashlib.sha256(b"other").hexdigest(), "segment_checksum_mismatch"),
    ],
)
def test_a_wrong_or_malformed_checksum_is_a_400(
    client: TestClient, tmp_path: Path, sha: str, code: str
) -> None:
    response = put_segment(client, new_visit(client), 0, b"the bytes", sha=sha)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == code
    assert staged_files(tmp_path) == []


def test_a_missing_checksum_is_a_400(client: TestClient) -> None:
    response = client.put(f"/api/visits/{new_visit(client)}/segments/0", content=b"the bytes")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "checksum_required"


def test_an_empty_segment_is_a_400(client: TestClient) -> None:
    response = put_segment(client, new_visit(client), 0, b"")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "empty_segment"


def test_a_segment_without_content_length_is_a_411(client: TestClient) -> None:
    def chunks():
        yield b"streamed with no length"

    response = client.put(
        f"/api/visits/{new_visit(client)}/segments/0",
        content=chunks(),
        headers={"X-Content-SHA256": hashlib.sha256(b"streamed with no length").hexdigest()},
    )

    assert response.status_code == 411
    assert response.json()["error"]["code"] == "length_required"


@pytest.mark.parametrize("seq", [-1, MAX_SEGMENTS_PER_VISIT, 10**30])
def test_a_seq_out_of_range_is_a_400_before_anything_is_written(
    client: TestClient, tmp_path: Path, seq: int
) -> None:
    response = put_segment(client, new_visit(client), seq, b"the bytes")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_seq"
    assert staged_files(tmp_path) == []
    assert not (tmp_path / "visits").exists()


def test_a_seq_that_isnt_a_number_is_a_422(client: TestClient) -> None:
    response = client.put(f"/api/visits/{new_visit(client)}/segments/first", content=b"x")

    assert response.status_code == 422


def test_a_segment_for_another_orgs_visit_is_a_404_and_leaves_nothing(
    client: TestClient, tmp_path: Path
) -> None:
    response = put_segment(client, other_orgs_visit(), 0, b"the bytes")

    assert response.status_code == 404
    assert staged_files(tmp_path) == []


def test_a_segment_for_a_completed_visit_is_a_409_and_leaves_nothing(
    client: TestClient, tmp_path: Path
) -> None:
    visit_id = new_visit(client)
    put_segment(client, visit_id, 0, b"zero")
    client.post(f"/api/visits/{visit_id}/complete", json={"last_seq": 0, "ext": "webm"})

    response = put_segment(client, visit_id, 1, b"one")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "visit_not_recording"
    assert staged_files(tmp_path) == []


# Raw ASGI: an HTTP client won't send a Content-Length that disagrees with its body.


def raw_put(
    app: FastAPI,
    path: str,
    headers: dict[str, str],
    chunks: list[bytes],
    disconnect_after: int | None = None,
) -> tuple:
    """Sends the request as a server would. Header values go as latin-1, as on the wire."""

    async def call() -> tuple:
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


def test_an_oversized_content_length_is_a_413_before_the_body_is_read(
    app: FastAPI, client: TestClient
) -> None:
    visit_id = new_visit(client)
    headers = {
        "content-length": str(MAX_SEGMENT_BYTES + 1),
        "x-content-sha256": "0" * 64,
    }

    status, body, chunks_read = raw_put(
        app, f"/api/visits/{visit_id}/segments/0", headers, [b"x" * 1024]
    )

    assert status == 413
    assert b"segment_too_large" in body
    assert chunks_read == 0


def test_a_body_longer_than_its_limit_is_a_413_even_if_the_header_says_less(
    app: FastAPI, client: TestClient, tmp_path: Path
) -> None:
    visit_id = new_visit(client)
    piece = b"x" * (1024 * 1024)
    chunks = [piece] * (MAX_SEGMENT_BYTES // len(piece) + 1)
    headers = {"content-length": "10", "x-content-sha256": "0" * 64}

    status, body, _ = raw_put(app, f"/api/visits/{visit_id}/segments/0", headers, chunks)

    assert status == 413
    assert b"segment_too_large" in body
    assert staged_files(tmp_path) == []


# --- complete ---------------------------------------------------------------------------------


def test_complete_with_every_segment_is_uploaded(client: TestClient) -> None:
    visit_id = new_visit(client)
    for seq in range(3):
        put_segment(client, visit_id, seq, f"part {seq}".encode())

    response = client.post(f"/api/visits/{visit_id}/complete", json={"last_seq": 2, "ext": "webm"})

    assert response.status_code == 200
    assert response.json()["status"] == "uploaded"
    assert response.json()["completed_at"].endswith("Z")


def test_complete_with_missing_segments_says_which(client: TestClient) -> None:
    visit_id = new_visit(client)
    put_segment(client, visit_id, 0, b"zero")
    put_segment(client, visit_id, 2, b"two")

    response = client.post(f"/api/visits/{visit_id}/complete", json={"last_seq": 2, "ext": "webm"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "segments_missing"
    assert response.json()["error"]["details"] == {"missing_seqs": [1]}


def test_complete_with_extra_segments_says_which(client: TestClient) -> None:
    visit_id = new_visit(client)
    for seq in range(2):
        put_segment(client, visit_id, seq, f"part {seq}".encode())

    response = client.post(f"/api/visits/{visit_id}/complete", json={"last_seq": 0, "ext": "webm"})

    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"unexpected_seqs": [1]}


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"last_seq": 0, "ext": "exe"}, 400, "unsupported_extension"),
        ({"last_seq": -1, "ext": "webm"}, 400, "invalid_seq"),
        ({"last_seq": 0}, 422, "invalid_request"),
        ({"last_seq": 0, "ext": "webm", "extra": 1}, 422, "invalid_request"),
    ],
)
def test_complete_of_the_wrong_shape_is_refused(
    client: TestClient, body: dict, status: int, code: str
) -> None:
    visit_id = new_visit(client)
    put_segment(client, visit_id, 0, b"zero")

    response = client.post(f"/api/visits/{visit_id}/complete", json=body)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_complete_twice_returns_the_same_visit(client: TestClient) -> None:
    visit_id = new_visit(client)
    put_segment(client, visit_id, 0, b"zero")
    body = {"last_seq": 0, "ext": "webm"}

    first = client.post(f"/api/visits/{visit_id}/complete", json=body).json()
    again = client.post(f"/api/visits/{visit_id}/complete", json=body)

    assert again.status_code == 200
    assert again.json() == first


# --- the current user, racing requests, and locks ---------------------------------------------


def test_without_the_demo_rep_requests_say_how_to_set_it_up(client: TestClient) -> None:
    with session_scope() as session:
        for visit in session.scalars(select(Visit)):
            session.delete(visit)
        rep = session.scalars(select(User).where(User.email == "rep@example.com")).one()
        session.delete(rep)

    response = client.get("/api/visits")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "demo_rep_missing"


def test_two_uploads_of_one_segment_with_different_bytes_store_one(app: FastAPI) -> None:
    with TestClient(app, raise_server_exceptions=False) as setup:
        visit_id = new_visit(setup)
    start = threading.Barrier(2, timeout=RACE_TIMEOUT_S)
    statuses: list[int] = []

    def send(body: bytes) -> None:
        with TestClient(app, raise_server_exceptions=False) as own:
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
        with TestClient(app, raise_server_exceptions=False) as own:
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


# --- found by review --------------------------------------------------------------------------


def test_a_content_length_in_other_digits_is_a_400_not_a_500(
    app: FastAPI, client: TestClient
) -> None:
    visit_id = new_visit(client)
    headers = {"content-length": "²", "x-content-sha256": "0" * 64}

    status, body, _ = raw_put(app, f"/api/visits/{visit_id}/segments/0", headers, [b"xx"])

    assert status == 400
    assert b"invalid_content_length" in body


@pytest.mark.parametrize("last_seq", [True, "0", 0.0])
def test_complete_takes_last_seq_only_as_a_whole_number(
    client: TestClient, last_seq: object
) -> None:
    visit_id = new_visit(client)
    put_segment(client, visit_id, 0, b"zero")

    response = client.post(
        f"/api/visits/{visit_id}/complete", json={"last_seq": last_seq, "ext": "webm"}
    )

    assert response.status_code == 422


def test_a_segment_for_a_malformed_visit_id_is_refused_before_the_body_is_read(
    app: FastAPI, tmp_path: Path
) -> None:
    body = b"the bytes"
    headers = {
        "content-length": str(len(body)),
        "x-content-sha256": hashlib.sha256(body).hexdigest(),
    }

    status, _body, chunks_read = raw_put(app, "/api/visits/not-a-uuid/segments/0", headers, [body])

    assert status == 422
    assert chunks_read == 0
    assert staged_files(tmp_path) == []


def test_without_a_current_user_no_segment_body_is_read(app: FastAPI, client: TestClient) -> None:
    visit_id = new_visit(client)
    with session_scope() as session:
        for visit in session.scalars(select(Visit)):
            session.delete(visit)
        session.delete(session.scalars(select(User).where(User.email == "rep@example.com")).one())
    body = b"the bytes"
    headers = {
        "content-length": str(len(body)),
        "x-content-sha256": hashlib.sha256(body).hexdigest(),
    }

    status, response, chunks_read = raw_put(
        app, f"/api/visits/{visit_id}/segments/0", headers, [body]
    )

    assert status == 500
    assert b"demo_rep_missing" in response
    assert chunks_read == 0


def test_an_upload_that_breaks_off_is_a_400_and_not_an_error_in_the_log(
    app: FastAPI, client: TestClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    visit_id = new_visit(client)
    headers = {"content-length": "2048", "x-content-sha256": "0" * 64}

    with caplog.at_level(logging.INFO):
        status, body, _ = raw_put(
            app, f"/api/visits/{visit_id}/segments/0", headers, [b"x" * 1024, b"x" * 1024], 1
        )

    assert status == 400
    assert b"upload_interrupted" in body
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert staged_files(tmp_path) == []
