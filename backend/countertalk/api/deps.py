"""Dependencies routes share."""

from typing import Annotated

from fastapi import Cookie, Depends

from countertalk import auth, users
from countertalk.db import session_scope


def current_user(
    session_cookie: Annotated[str | None, Cookie(alias=auth.COOKIE)] = None,
) -> users.CurrentUser:
    """The signed-in user, from the session cookie (batch 16); 401 signed_out without one.

    Looked up in its own short read-only session, so a write request's locks start at the
    route's first query, not here (§2.3).
    """
    token = auth.token_in(session_cookie)
    if token is None:
        raise auth.signed_out()
    with session_scope(read_only=True) as session:
        found = users.signed_in(session, token)
    if found is None:
        raise auth.signed_out()
    return found


CurrentUser = Annotated[users.CurrentUser, Depends(current_user)]
