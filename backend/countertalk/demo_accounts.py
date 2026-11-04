"""The demo accounts the sign-in page lists (batch F9; the user, 5 Nov), for anyone trying the
demo: who to sign in as. Listed only when DEMO_SIGNIN_PASSWORD is set, so local installs list
none, and only the accounts that exist and can sign in, so a seed without them lists nothing
wrong. Reps first, each with the visits it holds; then the other accounts by authority. Made-up
example.com addresses."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from countertalk.models import User


@dataclass(frozen=True)
class DemoAccount:
    email: str
    label: str
    # The languages of the account's visits: the reps'.
    note: str | None


REP = "Field sales rep"
ACCOUNTS = (
    DemoAccount("rep@example.com", REP, "Hindi, Hinglish and English"),
    DemoAccount("rep2@example.com", REP, "English and Marathi"),
    DemoAccount("rep3@example.com", REP, "English only"),
    DemoAccount("admin@example.com", "Admin", None),
    DemoAccount("manager@example.com", "Manager", None),
    DemoAccount("finance-member@example.com", "Finance team", None),
    DemoAccount("sales-ops-member@example.com", "Sales operations team", None),
    DemoAccount("distribution-member@example.com", "Distribution team", None),
)


def existing(session: Session) -> list[DemoAccount]:
    """ACCOUNTS that have a user with a password, in ACCOUNTS' order."""
    found = set(
        session.scalars(
            select(User.email).where(
                User.email.in_([account.email for account in ACCOUNTS]),
                User.password_hash.is_not(None),
            )
        )
    )
    return [account for account in ACCOUNTS if account.email in found]
