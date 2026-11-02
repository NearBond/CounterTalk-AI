"""Sign-in (batch 16): passwords, sessions, and a pause after wrong passwords.

Passwords are hashed with argon2 (argon2-cffi's defaults), ARGON2_AT_ONCE checks at a time. An
email with no account, or one with no password set, is still checked against a dummy hash, so
its answer takes as long and says the same: "Wrong email or password".

A session is a random token. The cookie holds it with its HMAC-SHA256 under SECRET_KEY; the
sessions table holds only its SHA-256, so a copy of the table signs nobody in. It lasts
SESSION_DAYS from sign-in and is never extended, as a GET never writes; signing out deletes it.

Wrong passwords in a row for one email (lowercased, whether or not an account has it) are
counted. From the (SIGNIN_FREE_FAILURES + 1)th on, a password is checked only once
min(failures - SIGNIN_FREE_FAILURES, SIGNIN_MAX_WAIT_S) seconds have passed since the last
check; until then an attempt is refused at once, 429 with Retry-After, the same for any email.
The check is claimed with one conditional UPDATE, so of attempts at once in a window, one
checks. A refusal checks no password and doesn't count, so a flood can't make the owner's pause
longer. A sign-in that works clears the count; a count untouched for SIGNIN_FAILURES_KEPT_H
starts again, and housekeeping deletes it, and expired sessions (prune). Never per IP: behind
the host's proxy, X-Forwarded-For is anyone's to set.

Passwords, tokens and hashes are never logged or put in an error (§1.9); nor are emails.
"""

import hashlib
import hmac
import logging
import math
import secrets
from datetime import datetime, timedelta
from functools import lru_cache

from anyio import CapacityLimiter, to_thread
from anyio.lowlevel import RunVar
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from countertalk import limits, rows
from countertalk.config import secret_key
from countertalk.db import session_scope
from countertalk.errors import Invalid, NotFound, SignedOut, TooMany
from countertalk.models import SigninFailure, User, UserSession, utc_now

log = logging.getLogger(__name__)

COOKIE = "countertalk_session"
_hasher = PasswordHasher()


def signed_out() -> SignedOut:
    return SignedOut("signed_out", "Sign in to carry on")


def missing_user() -> NotFound:
    return NotFound("user_not_found", "There's no such user")


def wrong_password() -> SignedOut:
    return SignedOut("wrong_email_or_password", "Wrong email or password")


def too_many_tries(seconds: int) -> TooMany:
    unit = "second" if seconds == 1 else "seconds"
    return TooMany(
        "too_many_tries",
        f"Too many tries for this email. Try again in {seconds} {unit}.",
        retry_after_s=seconds,
    )


# --- passwords -------------------------------------------------------------------------------


def normal_email(email: str) -> str:
    """As emails are stored (users' CHECK): trimmed and lowercase."""
    return email.strip().lower()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def set_password(session: Session, email: str, password: str) -> bool:
    """Sets the user's password and ends their sessions: False if no user has the email.
    Invalid if it's under PASSWORD_MIN_CHARS (never said back)."""
    if len(password) < limits.PASSWORD_MIN_CHARS:
        raise Invalid(
            "password_too_short",
            f"A password needs {limits.PASSWORD_MIN_CHARS} characters or more",
        )
    user = session.scalars(
        select(User).where(User.email == normal_email(email)).with_for_update()
    ).one_or_none()
    if user is None:
        return False
    user.password_hash = hash_password(password)
    session.execute(delete(UserSession).where(UserSession.user_id == user.id))
    return True


@lru_cache
def _dummy_hash() -> str:
    """Checked when there's no hash to check, so no account answers faster than a wrong one."""
    return _hasher.hash(secrets.token_urlsafe(16))


def password_matches(stored: str | None, password: str) -> bool:
    try:
        _hasher.verify(stored or _dummy_hash(), password)
    except (VerificationError, InvalidHashError):
        return False
    return stored is not None


def wait_for(failures: int) -> int:
    """Seconds after a check before the next, after this many wrong passwords in a row."""
    return min(max(failures - limits.SIGNIN_FREE_FAILURES, 0), limits.SIGNIN_MAX_WAIT_S)


# --- the cookie ------------------------------------------------------------------------------


@lru_cache
def _process_key() -> bytes:
    """With DEBUG on and no SECRET_KEY: a key for this process only (sessions end on restart).
    With DEBUG off, there is no signing without one (config.secret_key)."""
    return secrets.token_bytes(32)


def _key() -> bytes:
    key = secret_key()  # refuses an empty one with DEBUG off
    return key.encode() if key else _process_key()


def _signature(token: str) -> str:
    return hmac.new(_key(), token.encode(), hashlib.sha256).hexdigest()


def cookie_for(token: str) -> str:
    return f"{token}.{_signature(token)}"


def token_in(cookie: str | None) -> str | None:
    """The token, if the cookie is one we signed; None otherwise."""
    if not cookie:
        return None
    token, _, signature = cookie.rpartition(".")
    # As bytes: compare_digest refuses a str with a character past ASCII, and a cookie may
    # have one.
    if not token or not hmac.compare_digest(signature.encode(), _signature(token).encode()):
        return None
    return token


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- sessions --------------------------------------------------------------------------------


def start_session(session: Session, user: User) -> str:
    """A new session for the user: its token, for the cookie."""
    token = secrets.token_urlsafe(32)
    now = utc_now()
    session.add(
        UserSession(
            org_id=user.org_id,
            user_id=user.id,
            token_hash=token_hash(token),
            created_at=now,
            expires_at=now + timedelta(days=limits.SESSION_DAYS),
        )
    )
    return token


def user_of(session: Session, token: str) -> User | None:
    """The token's user while its session lasts; None once it has ended or never was."""
    return session.scalars(
        select(User)
        .join(UserSession, (UserSession.org_id == User.org_id) & (UserSession.user_id == User.id))
        .where(UserSession.token_hash == token_hash(token), UserSession.expires_at > utc_now())
    ).one_or_none()


def end_session(session: Session, token: str) -> None:
    session.execute(delete(UserSession).where(UserSession.token_hash == token_hash(token)))


# --- signing in ------------------------------------------------------------------------------


async def sign_in(email: str, password: str) -> tuple[str, str, str]:
    """The user's org and ID and a new session's token; SignedOut for a wrong email or
    password, or TooMany at once while the email's pause runs.

    Each step is its own transaction: a wrong password's count must stay although the answer
    is an error, which would roll back a request's one transaction."""
    email = normal_email(email)
    await to_thread.run_sync(_claim, email)
    found = await to_thread.run_sync(_account, email)
    stored = found[2] if found else None
    matches = await to_thread.run_sync(password_matches, stored, password, limiter=_argon2_slots())
    if not matches or found is None:
        await to_thread.run_sync(_note_failure, email)
        log.info("sign-in refused")
        raise wrong_password()
    org_id, user_id = found[0], found[1]
    token = await to_thread.run_sync(_signed_in, org_id, user_id, email)
    log.info("signed in user=%s", user_id)
    return org_id, user_id, token


def prune(session: Session) -> dict[str, int]:
    """Housekeeping: expired sessions, and failure counts untouched for SIGNIN_FAILURES_KEPT_H."""
    now = utc_now()
    ended = session.execute(delete(UserSession).where(UserSession.expires_at <= now))
    forgotten = session.execute(
        delete(SigninFailure).where(SigninFailure.last_checked_at < _kept_since(now))
    )
    return {"sessions_ended": ended.rowcount, "signin_failures_forgotten": forgotten.rowcount}


_ARGON2: RunVar[CapacityLimiter] = RunVar("argon2")


def _argon2_slots() -> CapacityLimiter:
    """ARGON2_AT_ONCE checks at a time, each using argon2's 64 MiB: sign-ins past them wait
    their turn, holding no thread. One per event loop, as anyio's own thread limiter."""
    try:
        return _ARGON2.get()
    except LookupError:
        slots = CapacityLimiter(limits.ARGON2_AT_ONCE)
        _ARGON2.set(slots)
        return slots


def _kept_since(now: datetime) -> datetime:
    return now - timedelta(hours=limits.SIGNIN_FAILURES_KEPT_H)


def _claim(email: str) -> None:
    """Lets this attempt check a password, or refuses it at once (TooMany) while the email's
    pause runs. One conditional UPDATE claims the check, so of two attempts at once in a window
    one checks; the claim restarts the window from now. A count untouched for
    SIGNIN_FAILURES_KEPT_H is forgotten first."""
    now = utc_now()
    pause = func.least(
        func.greatest(SigninFailure.failures - limits.SIGNIN_FREE_FAILURES, 0),
        limits.SIGNIN_MAX_WAIT_S,
    )
    with session_scope() as session:
        session.execute(
            delete(SigninFailure).where(
                SigninFailure.email == email, SigninFailure.last_checked_at < _kept_since(now)
            )
        )
        claimed = session.execute(
            update(SigninFailure)
            .where(
                SigninFailure.email == email,
                or_(
                    pause == 0,
                    SigninFailure.last_checked_at + func.make_interval(0, 0, 0, 0, 0, 0, pause)
                    <= now,
                ),
            )
            .values(last_checked_at=now)
            .returning(SigninFailure.email)
        ).one_or_none()
        if claimed is not None:
            return
        found = session.get(SigninFailure, email)
        if found is None:
            return  # no wrong passwords: nothing to wait for
        left = found.last_checked_at + timedelta(seconds=wait_for(found.failures)) - now
    raise too_many_tries(max(math.ceil(left.total_seconds()), 1))


def _account(email: str) -> tuple[str, str, str | None] | None:
    """The user with this email: their org, ID and password hash."""
    with session_scope(read_only=True) as session:
        row = session.execute(
            select(User.org_id, User.id, User.password_hash).where(User.email == email)
        ).one_or_none()
        return (row[0], row[1], row[2]) if row else None


def _note_failure(email: str) -> None:
    """One more wrong password for the email, counted in the database so two at once both count."""
    now = utc_now()
    with session_scope() as session:
        session.execute(
            insert(SigninFailure)
            .values(email=email, failures=1, last_checked_at=now)
            .on_conflict_do_update(
                index_elements=[SigninFailure.email],
                set_={"failures": SigninFailure.failures + 1, "last_checked_at": now},
            )
        )


def _signed_in(org_id: str, user_id: str, email: str) -> str:
    with session_scope() as session:
        session.execute(delete(SigninFailure).where(SigninFailure.email == email))
        return start_session(session, rows.find_one(session, User, org_id, user_id, missing_user()))
