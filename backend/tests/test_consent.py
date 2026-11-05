"""Consent to recording, given before any visit (batch 17): the link a rep shares, the paper
form, the shop's public page, and the server's check before a conversation (§7.4)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from countertalk import consent
from countertalk.db import session_scope
from countertalk.models import Consent, ConsentLink, Role, Shop, Visit, VisitStatus
from countertalk.playbooks import PackError
from tests.conftest import a_user, sign_in
from tests.test_review_api import signed_in_demo
from tests.test_visits import Org_, make_org, sha

NEEDED = {
    "code": "consent_needed",
    "message": "This shop hasn't agreed to recording yet: share its consent link, or mark the"
    " paper form it signed",
}
NOT_FOUND = {"error": {"code": "not_found", "message": "Not Found"}}


def unanswered_shop(org: Org_, name: str = "New Shop") -> str:
    with session_scope() as session:
        shop = Shop(org_id=org.org_id, name=name, area="New Area")
        session.add(shop)
        session.flush()
        return shop.id


def start(client: TestClient, shop_id: str, kind: str = "conversation") -> dict:
    response = client.post("/api/visits", json={"shop_id": shop_id, "kind": kind, "client": "web"})
    return {**response.json(), "http": response.status_code}


def share(client: TestClient, shop_id: str) -> str:
    """The token of a new link to the shop's consent page."""
    response = client.post(f"/api/shops/{shop_id}/consent-links")
    assert response.status_code == 201
    path = response.json()["path"]
    assert path.startswith("/consent/")
    return path.removeprefix("/consent/")


def answer(client: TestClient, token: str, decision: str, language: str = "mr") -> dict:
    # The shopkeeper isn't signed in: the page is public.
    with TestClient(client.app, base_url="https://testserver") as public:
        response = public.post(
            f"/api/consent/{token}", json={"decision": decision, "language": language}
        )
    assert response.status_code == 200
    return response.json()


def shop_consent(client: TestClient, shop_id: str) -> str | None:
    [found] = [s for s in client.get("/api/shops").json()["items"] if s["id"] == shop_id]
    return found["consent"]


@pytest.fixture
def org(client: TestClient) -> Org_:
    found = make_org()
    sign_in(client, found.rep_id)
    return found


def test_a_conversation_needs_a_yes_but_notes_dont(client: TestClient, org: Org_) -> None:
    shop_id = unanswered_shop(org)

    refused = start(client, shop_id)

    assert (refused["http"], refused["error"]) == (409, NEEDED)
    assert shop_consent(client, shop_id) is None
    assert start(client, shop_id, kind="notes")["http"] == 201


def test_a_yes_on_the_page_lets_the_rep_record(client: TestClient, org: Org_) -> None:
    shop_id = unanswered_shop(org)
    token = share(client, shop_id)

    page = client.get(f"/api/consent/{token}").json()
    after = answer(client, token, "yes")

    # Only the company's and the shop's names, and its answer (no pack: the org's own name).
    assert page.pop("notice") == client.get(f"/api/consent/{token}").json()["notice"]
    after.pop("notice")
    assert page == {
        "company": "Visit Test Org",
        "shop": "New Shop",
        "decision": None,
        "notice_version": 1,
    }
    assert after == page | {"decision": "yes"}
    assert shop_consent(client, shop_id) == "yes"
    assert start(client, shop_id)["http"] == 201


def test_the_page_names_the_company_by_its_pack(client: TestClient) -> None:
    demo = signed_in_demo(client)
    shop_id = unanswered_shop(demo)

    page = client.get(f"/api/consent/{share(client, shop_id)}").json()

    assert (page["company"], page["shop"]) == ("PepsiCo India", "New Shop")


def test_a_no_blocks_new_recordings_and_the_rest_of_a_started_one(
    client: TestClient, org: Org_
) -> None:
    started = start(client, org.shop_id)  # make_org's shop agreed on paper
    token = share(client, org.shop_id)

    assert answer(client, token, "no")["decision"] == "no"

    refused = start(client, org.shop_id)
    segment = client.put(
        f"/api/visits/{started['id']}/segments/0",
        content=b"segment bytes",
        headers={"x-content-sha256": sha(b"segment bytes")},
    )
    assert (refused["http"], refused["error"]) == (409, NEEDED)
    assert (segment.status_code, segment.json()["error"]) == (409, NEEDED)


def test_a_shop_can_change_its_mind_through_any_link_it_was_sent(
    client: TestClient, org: Org_
) -> None:
    shop_id = unanswered_shop(org)
    first, second = share(client, shop_id), share(client, shop_id)

    answer(client, first, "no")
    answer(client, second, "yes")
    again = client.get(f"/api/consent/{first}").json()

    assert first != second
    assert (again["decision"], shop_consent(client, shop_id)) == ("yes", "yes")
    with session_scope(read_only=True) as session:
        decisions = session.scalars(
            select(Consent.decision).where(Consent.shop_id == shop_id).order_by(Consent.seq)
        ).all()
        hashes = session.scalars(select(ConsentLink.token_hash)).all()
    assert decisions == ["no", "yes"]  # every answer is kept
    assert first not in hashes and second not in hashes  # only their hashes are


@pytest.mark.parametrize(
    "token",
    ["", "short", "A" * 43, "A" * 44, "with/a/slash", "a token with spaces"],
    ids=["empty", "short", "unknown", "too-long", "a-path", "spaces"],
)
def test_a_token_that_isnt_a_shop_s_gets_the_404_of_any_path(
    client: TestClient, org: Org_, token: str
) -> None:
    share(client, unanswered_shop(org))  # a shop with a link, which none of these is

    read = client.get(f"/api/consent/{token}")
    written = client.post(f"/api/consent/{token}", json={"decision": "yes", "language": "hi"})

    assert (read.status_code, read.json()) == (404, NOT_FOUND)
    assert (written.status_code, written.json()) == (404, NOT_FOUND)
    with session_scope(read_only=True) as session:
        assert session.scalars(select(Consent).where(Consent.method == "link")).all() == []


def test_a_link_answers_for_its_own_shop_only(client: TestClient, org: Org_) -> None:
    mine, other = unanswered_shop(org), unanswered_shop(org, "Other Shop")

    answer(client, share(client, mine), "yes")

    assert (shop_consent(client, mine), shop_consent(client, other)) == ("yes", None)


def test_an_answer_that_isnt_one_is_refused(client: TestClient, org: Org_) -> None:
    token = share(client, unanswered_shop(org))

    response = client.post(f"/api/consent/{token}", json={"decision": "yes", "language": "ta"})

    assert (response.status_code, response.json()["error"]["code"]) == (422, "invalid_request")


@pytest.mark.parametrize("role", [Role.REP, Role.MANAGER, Role.ADMIN])
def test_a_paper_form_is_a_yes_marked_by_who_marked_it(
    client: TestClient, org: Org_, role: str
) -> None:
    shop_id = unanswered_shop(org)
    marker = a_user(org.org_id, role, f"Marker {role}")
    sign_in(client, marker)

    response = client.post(f"/api/shops/{shop_id}/consent", json={"language": "mr"})

    assert (response.status_code, response.json()) == (201, {"consent": "yes"})
    with session_scope(read_only=True) as session:
        [row] = session.scalars(select(Consent).where(Consent.shop_id == shop_id)).all()
    assert (row.decision, row.method, row.recorded_by, row.language, row.notice_version) == (
        "yes",
        "paper",
        marker,
        "mr",
        1,
    )


def test_marking_unanswered_shops_never_changes_an_answer(client: TestClient, org: Org_) -> None:
    said_no, waiting = unanswered_shop(org, "Said No"), unanswered_shop(org, "Waiting")
    answer(client, share(client, said_no), "no")

    with session_scope() as session:
        marked = consent.mark_unanswered(session, org.org_id, org.rep_id, "hi")

    assert marked == 1
    assert (shop_consent(client, said_no), shop_consent(client, waiting)) == ("no", "yes")


# --- the database's own rules ----------------------------------------------------------------


def insert(org: Org_, shop_id: str, decision: str, method: str, recorded_by: str | None) -> None:
    with session_scope() as session:
        session.add(
            Consent(
                org_id=org.org_id,
                shop_id=shop_id,
                decision=decision,
                method=method,
                notice_version=1,
                language="hi",
                recorded_by=recorded_by,
            )
        )


@pytest.mark.parametrize(
    ("decision", "method", "by_rep", "check"),
    [
        ("no", "paper", True, "ck_consents_paper_is_yes"),
        ("yes", "paper", False, "ck_consents_recorded_by"),
        ("yes", "link", True, "ck_consents_recorded_by"),
    ],
    ids=["paper-no", "paper-unmarked", "link-marked"],
)
def test_a_consent_the_rules_dont_allow_is_refused(
    org: Org_, decision: str, method: str, by_rep: bool, check: str
) -> None:
    shop_id = unanswered_shop(org)

    with pytest.raises(DBAPIError) as refused:
        insert(org, shop_id, decision, method, org.rep_id if by_rep else None)

    assert check in str(refused.value.orig)


@pytest.mark.parametrize(
    "statement",
    ["UPDATE consents SET decision = 'no'", "DELETE FROM consents"],
    ids=["update", "delete"],
)
def test_the_answers_are_never_changed(org: Org_, statement: str) -> None:
    with pytest.raises(DBAPIError), session_scope() as session:
        session.execute(text(statement))

    with session_scope(read_only=True) as session:
        assert session.scalars(select(Consent.decision)).all() == ["yes"]  # make_org's paper yes


def test_a_confirmed_visit_has_a_confirmed_time_and_only_it_does(org: Org_) -> None:
    with pytest.raises(DBAPIError) as refused, session_scope() as session:
        session.add(
            Visit(
                org_id=org.org_id,
                shop_id=org.shop_id,
                rep_id=org.rep_id,
                kind="conversation",
                client="upload",
                status=VisitStatus.CONFIRMED,
            )
        )

    assert "ck_visits_confirmed_at" in str(refused.value.orig)


def test_a_paper_form_can_t_overrule_a_no_given_on_the_page(client: TestClient, org: Org_) -> None:
    shop_id = unanswered_shop(org)
    answer(client, share(client, shop_id), "no")

    response = client.post(f"/api/shops/{shop_id}/consent", json={"language": "hi"})

    assert (response.status_code, response.json()["error"]["code"]) == (409, "consent_refused")
    assert shop_consent(client, shop_id) == "no"


# --- the notice (consent/v1) -----------------------------------------------------------------


def test_the_notice_is_in_each_consent_language_with_every_part() -> None:
    found = consent.notice()

    assert list(found) == ["hi", "en", "mr"]
    for words in found.values():
        assert len(words.benefits) == 3
        assert "30" in words.retention  # days
    assert "real-time assistance" in found["en"].real_time


def test_the_page_carries_the_notice_and_a_link_its_message(client: TestClient, org: Org_) -> None:
    shop_id = unanswered_shop(org)
    shared = client.post(f"/api/shops/{shop_id}/consent-links").json()

    page = client.get(f"/api/consent/{shared['path'].removeprefix('/consent/')}").json()

    assert list(page["notice"]) == ["hi", "en", "mr"]
    assert page["notice"]["mr"]["agree"] == "मी सहमत आहे"
    assert "share_message" not in page["notice"]["en"]
    assert shared["message"].splitlines() == [
        words.share_message for words in consent.notice().values()
    ]


@pytest.mark.parametrize(
    ("old", "new", "field"),
    [
        ("version: 1\n", "version: 2\n", "version"),
        ("  mr:\n    name: मराठी\n", "  ta:\n    name: मराठी\n", "languages"),
        ("    agree: I agree\n", "", "languages.en.agree"),
    ],
    ids=["version", "language", "missing"],
)
def test_a_notice_that_doesnt_check_out_names_its_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, old: str, new: str, field: str
) -> None:
    broken = tmp_path / "notice.yaml"
    text = consent.NOTICE_PATH.read_text(encoding="utf-8")
    assert old in text
    broken.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")
    monkeypatch.setattr(consent, "NOTICE_PATH", broken)
    consent.notice.cache_clear()

    try:
        with pytest.raises(PackError) as error:
            consent.notice()
    finally:
        consent.notice.cache_clear()

    assert error.value.field == field
