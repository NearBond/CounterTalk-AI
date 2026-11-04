"""Writes pre-visit briefs with Muse Spark: a dev tool (batch F5's paid check).

    uv run python scripts/write_brief.py <org_id> [--shops 3] [--as <email>] [--yes]

The shops are the org's that can have a new brief now (2 confirmed visits or more, and none yet
or a confirmed visit since), the most confirmed visits first, at most --shops of them.

Without --yes it picks each shop's facts and builds its request as the app would, and prints
each shop, its confirmed visits and state, and an estimate of the tokens (reasoning counted) and
the cost at Muse Spark's prices. It sends nothing and writes nothing.

With --yes each shop's brief is written as POST /api/shops/{id}/brief does (brief.generate), as
the org's first manager or admin by email (or --as <email>; a brief keeps its writer, who can't
be deleted while it stands), with MODEL_API_KEY from backend/.env: one call a shop, logged in
llm_calls with its brief. A call that fails stops the run. Every exit prints what was paid.

It prints counts and costs: never the words or the quotes. Read a brief at /briefs/<shop id>.
"""

import argparse
import sys
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select

from countertalk import brief, extraction
from countertalk.db import SETUP_ERRORS, session_scope, setup_trouble
from countertalk.errors import DomainError, describe
from countertalk.llm.base import CallDetails, Message, Outcome, StructuredLlm
from countertalk.logs import configure_logging
from countertalk.models import Org, Role, User, parse_id
from countertalk.users import CurrentUser
from scripts._paid import estimate, model_llm, show_calls

# A dry run's estimate of a brief's answer, reasoning added on top: up to six short sentences.
BRIEF_ANSWER_TOKENS = 300
SHOPS_DEFAULT = 3


class Counted:
    """The LLM, with every call it made kept, for what was paid on the way out."""

    def __init__(self, llm: StructuredLlm) -> None:
        self.llm = llm
        self.provider = llm.provider
        self.model = llm.model
        self.calls: list[CallDetails] = []

    def complete(
        self, messages: Sequence[Message], name: str, schema: dict[str, Any], **given: Any
    ) -> Outcome:
        outcome = self.llm.complete(messages, name, schema, **given)
        self.calls.extend(outcome.calls)
        return outcome


def shops_for(org_id: str, most: int) -> list[brief.Standing]:
    with session_scope(read_only=True) as session:
        found = [shop for shop in brief.every_shop(session, org_id) if shop.can_generate]
    return sorted(found, key=lambda shop: shop.visits, reverse=True)[:most]


def lead_of(org_id: str, email: str | None) -> tuple[CurrentUser, str] | None:
    """Who the briefs are written as: the org's manager or admin with that email, or its first by
    email. A brief keeps its writer, who then can't be deleted while it stands."""
    with session_scope(read_only=True) as session:
        query = select(User).where(User.org_id == org_id, User.role.in_([Role.MANAGER, Role.ADMIN]))
        if email is not None:
            query = query.where(User.email == email)
        found = session.scalars(query.order_by(User.email)).first()
        if found is None:
            return None
        return CurrentUser(found.id, found.org_id, found.role, None), found.email


def dry_run(org_id: str, shops: list[brief.Standing]) -> int:
    total = 0.0
    with session_scope(read_only=True) as session:
        pack = extraction.pack_for(org_id, session=session)
        for shop in shops:
            picked = brief.pick(session, pack, org_id, shop)
            found = estimate(brief.messages(picked.facts), brief.schema(), BRIEF_ANSWER_TOKENS)
            total += found.usd
            print(
                f"{shop.shop_id} {shop.name}, {shop.area}: {shop.visits} confirmed visits,"
                f" {shop.state}; {found.tokens_in:,} tokens in, about {found.tokens_out:,} out,"
                f" about ${found.usd:.4f}"
            )
    print(f"{len(shops)} briefs, about ${total:.4f}. Nothing sent: add --yes to write them.")
    return 0


def run(org_id: str, shops: list[brief.Standing], llm: StructuredLlm, email: str | None) -> int:
    lead = lead_of(org_id, email)
    if lead is None:
        print(f"org {org_id} has no such manager or admin to write them as", file=sys.stderr)
        return 1
    user, writer = lead
    print(f"writing as {writer}")
    counted = Counted(llm)
    try:
        for shop in shops:
            found = brief.generate(counted, user, shop.shop_id)
            saved = found.brief
            assert saved is not None
            content = saved.content
            kept = len(content["overview"]) + len(content["worth_raising"])
            print(
                f"{shop.shop_id} {shop.name}: brief {saved.id}, after visit {saved.visits},"
                f" {kept} sentences kept, dropped {content['dropped']}"
            )
    finally:
        show_calls(counted.calls)
    return 0


def main(argv: list[str], llm: StructuredLlm | None = None) -> int:
    parser = argparse.ArgumentParser(prog="write_brief.py")
    parser.add_argument("org_id")
    parser.add_argument("--shops", type=int, default=SHOPS_DEFAULT, help="at most this many")
    parser.add_argument("--yes", action="store_true", help="send them, and pay")
    parser.add_argument(
        "--as", dest="email", help="the manager's or admin's email to write them as"
    )
    args = parser.parse_args(argv)
    org_id = parse_id(args.org_id)
    if org_id is None:
        parser.error("that isn't an org ID")
    if args.shops < 1:
        parser.error("--shops must be 1 or more")
    try:
        with session_scope(read_only=True) as session:
            if session.get(Org, org_id) is None:
                print(f"org {org_id} doesn't exist", file=sys.stderr)
                return 1
        shops = shops_for(org_id, args.shops)
        if not shops:
            print("no shop can have a new brief now (2 confirmed visits, and one since the last)")
            return 0
        if not args.yes:
            return dry_run(org_id, shops)
        llm = llm or model_llm()
        if llm is None:
            return 1
        return run(org_id, shops, llm, args.email)
    except SETUP_ERRORS as error:
        print(f"failed: {setup_trouble(error)}", file=sys.stderr)
        return 1
    except DomainError as error:  # a failed call, or a pack that doesn't check out
        print(f"failed: {describe(error)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    configure_logging()  # each call's tokens, never the words
    sys.exit(main(sys.argv[1:]))
