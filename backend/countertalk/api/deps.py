"""Dependencies routes share."""

from typing import Annotated

from fastapi import Depends

from countertalk import users
from countertalk.config import get_settings
from countertalk.db import session_scope


def current_user() -> users.CurrentUser:
    """The demo rep until batch 16, or in development DEV_USER_EMAIL's user (batch 15b).

    Looked up in its own short read-only session, so a write request's locks start at the
    route's first query, not here (§2.3).
    """
    email = get_settings().DEV_USER_EMAIL
    with session_scope(read_only=True) as session:
        return users.dev_user(session, email) if email else users.demo_rep(session)


CurrentUser = Annotated[users.CurrentUser, Depends(current_user)]
