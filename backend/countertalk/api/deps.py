"""Dependencies routes share."""

from typing import Annotated

from fastapi import Depends

from countertalk import users
from countertalk.db import session_scope


def current_user() -> users.CurrentUser:
    """The demo rep until batch 16.

    Looked up in its own short read-only session, so a write request's locks start at the
    route's first query, not here (§2.3).
    """
    with session_scope(read_only=True) as session:
        return users.demo_rep(session)


CurrentUser = Annotated[users.CurrentUser, Depends(current_user)]
