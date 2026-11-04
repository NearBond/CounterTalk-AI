"""Seeds the demo org, its three reps and its shops: `uv run python scripts/seed_dev.py`.

The demo org runs on the PepsiCo India example pack (batch 8): its teams, distributors and
catalog are imported, and the shops are spread across the distributors and their beats. Each
shop that hasn't answered gets a paper consent yes, marked by Demo Rep (batch 17). Then, once,
a problem at Sunrise Kirana that its team has fixed since that visit, so the shop pick shows
"Fixed since the last visit here" at once (batch 17b), made through K6's own visit and resolve.
Its finding is the marker, so a rerun never adds a visit, even after its issue reopens. It
never touches a real issue: when the team already has one it would join (the same category
and distributor), it's skipped. Not guarded against two runs at once, which could make it
twice.

Safe to run again, even twice at once: rows are matched on their natural keys, and existing
ones are left exactly as they are. A shop's distributor, and its beat, are each filled in only
while it has none. Every name is made up except the org's, the pack's company (5 Nov), and the
reps' addresses are example.com.
"""

import sys
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from countertalk import consent, issues, routing
from countertalk.catalog import import_org_pack
from countertalk.db import SETUP_ERRORS, session_scope, setup_trouble
from countertalk.errors import Conflict, DomainError, describe
from countertalk.logs import configure_logging
from countertalk.models import (
    Base,
    Distributor,
    Insight,
    Issue,
    IssueStatus,
    Org,
    Role,
    Shop,
    User,
    ist_date,
    utc_now,
)
from countertalk.playbooks import OrgPack, load_org_pack
from scripts import _k6

DEMO_ORG_NAME = "PepsiCo India"
# The dev org's rep: scripts/set_password.py gives them a password to sign in with.
DEMO_REP = {"email": "rep@example.com", "name": "Demo Rep"}
# Two more reps, seeded the same way (4 and 5 Nov): the demo's visits come from three people.
DEMO_REP_2 = {"email": "rep2@example.com", "name": "Demo Rep 2"}
DEMO_REP_3 = {"email": "rep3@example.com", "name": "Demo Rep 3"}
DEMO_PACK = ("pepsico-india-demo", 1)
# Batch 17b's example: a problem raised three days ago, fixed since. Made-up words.
DEMO_FIXED = {
    "shop": "Sunrise Kirana",
    "rep": "rep",
    "pattern": "fixed",
    "kind": "problem",
    "category": "supply",
    "severity": "medium",
    "summary": "The last two Kurkure deliveries came a week late.",
    "quote": "Kurkure ki delivery do baar ek hafta late aayi.",
    "team": "distribution",
    "note": "Your distributor now delivers every Monday, so the next one comes on time.",
}
DEMO_FIXED_DAYS_AGO = 3
# What seed_fixed did, as main() prints it.
FIXED_MADE = "made at Sunrise Kirana"
FIXED_THERE = "already there"
FIXED_SKIPPED = (
    "skipped: the distribution team already has a supply issue for Sunrise Kirana's"
    " distributor, so a made-up example would change a real one"
)
# Who fixed it: the distribution team's member, with no password (made up).
DEMO_RESOLVER = {
    "email": "distribution-member@example.com",
    "name": "Distribution Member",
}
# The language of the paper forms the seeded shops signed.
SEEDED_CONSENT_LANGUAGE = "hi"
# (name, area, distributor key, beat): fictional kirana shops in made-up areas, on the pack's
# made-up distributors.
DEMO_SHOPS = [
    ("Sunrise Kirana", "Old Market", "d1", "Old Market Monday"),
    ("Lotus General Store", "Old Market", "d2", "Old Market Thursday"),
    ("Corner Daily Needs", "Station Road", "d3", "Station Road Tuesday"),
    ("Moonlight Provisions", "Riverside", "d3", "Riverside Friday"),
    ("Green Leaf Mart", "Lake View", "d1", "Lake View Wednesday"),
    ("Anand Provision Store", "Lake View", "d1", "Lake View Wednesday"),
    ("Royal Super Store", "Mill Road", "d2", "Mill Road Saturday"),
    ("Bharat General Store", "Mill Road", "d2", "Mill Road Saturday"),
    ("Sai Krupa Stores", "Riverside", "d3", "Riverside Friday"),
    ("Hari Om Provisions", "Station Road", "d3", "Station Road Tuesday"),
    ("Shubham General Store", "Park Road", "d3", "Park Road Thursday"),
]


@dataclass(frozen=True)
class SeedResult:
    created: int
    existing: int


def seed(session: Session, pack: OrgPack) -> SeedResult:
    """The demo org, its reps and shops, on `pack` (load it before the transaction)."""
    inserted = [_insert_missing(session, Org, {"name": DEMO_ORG_NAME}, ["name"])]
    org_id = session.scalars(select(Org.id).where(Org.name == DEMO_ORG_NAME)).one()
    rep_ids: list[str] = []
    for values in (DEMO_REP, DEMO_REP_2, DEMO_REP_3):
        rep = {"org_id": org_id, "role": Role.REP.value, **values}
        rep_id, made = _seeded_user(session, rep, "demo_rep_in_another_org")
        rep_ids.append(rep_id)
        inserted.append(made)
    for name, area, _, _ in DEMO_SHOPS:
        shop = {"org_id": org_id, "name": name, "area": area}
        inserted.append(_insert_missing(session, Shop, shop, ["org_id", "name", "area"]))
    import_org_pack(session, org_id, pack)
    distributors = dict(
        session.execute(
            select(Distributor.key, Distributor.id).where(Distributor.org_id == org_id)
        ).all()
    )
    for name, area, distributor, beat in DEMO_SHOPS:
        this_shop = (Shop.org_id == org_id, Shop.name == name, Shop.area == area)
        session.execute(
            update(Shop)
            .where(*this_shop, Shop.distributor_id.is_(None))
            .values(distributor_id=distributors[distributor])
        )
        session.execute(update(Shop).where(*this_shop, Shop.beat.is_(None)).values(beat=beat))
    # Batch 17: the demo records at once, so its shops agreed on paper, marked by its first rep.
    # A shop's own answer since is never changed.
    consent.mark_unanswered(session, org_id, rep_ids[0], SEEDED_CONSENT_LANGUAGE)
    return SeedResult(created=sum(inserted), existing=len(inserted) - sum(inserted))


def seed_fixed(pack: OrgPack) -> str:
    """Batch 17b's example, once, after seed() has committed: what happened, to print."""
    with session_scope() as session:
        org_id = session.scalars(select(Org.id).where(Org.name == DEMO_ORG_NAME)).one()
        made = session.scalars(
            select(Insight.id).where(
                Insight.org_id == org_id, Insight.summary_en == DEMO_FIXED["summary"]
            )
        ).first()
        if made is not None:
            return FIXED_THERE
        teams = routing.team_ids(session, org_id)
        team_id = teams[DEMO_FIXED["team"]]
        shop = session.scalars(
            select(Shop).where(Shop.org_id == org_id, Shop.name == DEMO_FIXED["shop"])
        ).one()
        # The issue the example's finding would join (it names no product): a real one is
        # never joined, reopened or resolved by a made-up example.
        category = DEMO_FIXED["category"]
        fields = issues.key_fields(pack.playbook.categories[category].group_by, Insight(), shop)
        taken = session.scalars(
            select(Issue.id).where(
                Issue.org_id == org_id,
                Issue.team_id == team_id,
                Issue.issue_key == issues.issue_key(category, fields),
                Issue.status != IssueStatus.MERGED,
            )
        ).first()
        if taken is not None:
            return FIXED_SKIPPED
        resolver = {
            "org_id": org_id,
            "role": Role.TEAM_MEMBER.value,
            "team_id": team_id,
            **DEMO_RESOLVER,
        }
        resolver_id, _ = _seeded_user(session, resolver, "demo_resolver_elsewhere")
        rep_id = session.scalars(
            select(User.id).where(User.org_id == org_id, User.email == DEMO_REP["email"])
        ).one()
        shop_id = shop.id
    world = _k6.World(
        org_id, pack, ist_date(utc_now()), teams, resolver_id, {DEMO_FIXED["shop"]: shop_id}
    )
    when = utc_now() - timedelta(days=DEMO_FIXED_DAYS_AGO)
    _k6._visit(world, {"rep": rep_id}, DEMO_FIXED, when)  # noqa: SLF001
    _k6._resolve(world, resolver_id, DEMO_FIXED)  # noqa: SLF001
    return FIXED_MADE


def _seeded_user(session: Session, values: dict[str, Any], code: str) -> tuple[str, int]:
    """The user, inserted unless their email is taken: (their ID, 1 if inserted now).
    Emails are unique across orgs, so an address already in another org or team is refused,
    never used in its place."""
    made = _insert_missing(session, User, values, ["email"])
    found = session.execute(
        select(User.id, User.org_id, User.team_id).where(User.email == values["email"])
    ).one()
    if (found.org_id, found.team_id) != (values["org_id"], values.get("team_id")):
        raise Conflict(code, f"{values['email']} is in another org or team")
    return found.id, made


def _insert_missing(
    session: Session, model: type[Base], values: dict[str, Any], natural_key: list[str]
) -> int:
    """Inserts the row unless its natural key is taken. Returns 1 if it inserted."""
    table = model.__table__
    statement = (
        insert(table)
        .values(**values)
        .on_conflict_do_nothing(index_elements=natural_key)
        # Only a row actually inserted comes back.
        .returning(table.c.id)
    )
    return len(session.execute(statement).all())


def main() -> int:
    try:
        pack = load_org_pack(*DEMO_PACK)  # before the transaction: no lock waits on file reads
        with session_scope() as session:
            result = seed(session, pack)
    except SETUP_ERRORS as error:
        print(f"can't seed: {setup_trouble(error)}", file=sys.stderr)
        return 1
    except DomainError as error:
        print(f"can't seed: {describe(error)}", file=sys.stderr)
        return 1
    print(f"seeded: created {result.created}, already there {result.existing}")
    try:
        fixed = seed_fixed(pack)
    except SETUP_ERRORS as error:
        print(f"can't seed the fixed example: {setup_trouble(error)}", file=sys.stderr)
        return 1
    except DomainError as error:
        print(f"can't seed the fixed example: {describe(error)}", file=sys.stderr)
        return 1
    print(f"fixed since the last visit: {fixed}")
    return 0


if __name__ == "__main__":
    configure_logging()
    sys.exit(main())
