"""Sign-in, sign-out and who is signed in (batch 16). The rules are countertalk.auth's."""

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Cookie, Response, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from countertalk import auth, limits, users
from countertalk.api.deps import CurrentUser
from countertalk.api.issues import TeamOut
from countertalk.config import get_settings
from countertalk.db import DbSession, session_scope

router = APIRouter(prefix="/api/auth", tags=["auth"])


class SignInIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class MeOut(BaseModel):
    id: str
    name: str
    email: str
    role: str
    # A team member's team; None for every other role.
    team: TeamOut | None
    org_name: str
    # Whether MODEL_API_KEY is set (batch F8): without it, the app shows a warning bar. Only
    # whether: no part of the key is ever sent.
    ai_key_set: bool


def _me_out(found: users.Me) -> MeOut:
    key_set = bool(get_settings().MODEL_API_KEY.get_secret_value())
    return MeOut.model_validate({**asdict(found), "ai_key_set": key_set})


def _me(org_id: str, user_id: str) -> MeOut:
    with session_scope(read_only=True) as session:
        return _me_out(users.me(session, org_id, user_id))


@router.post("/signin")
async def sign_in(body: SignInIn, response: Response) -> MeOut:
    """Async, so a wait after wrong passwords holds no thread (auth.sign_in). 401
    wrong_email_or_password, the same whether or not the email has an account."""
    org_id, user_id, token = await auth.sign_in(body.email, body.password)
    me = await run_in_threadpool(_me, org_id, user_id)
    response.set_cookie(
        auth.COOKIE,
        auth.cookie_for(token),
        max_age=limits.SESSION_DAYS * 24 * 60 * 60,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return me


@router.post("/signout", status_code=status.HTTP_204_NO_CONTENT)
def sign_out(
    session: DbSession,
    response: Response,
    session_cookie: Annotated[str | None, Cookie(alias=auth.COOKIE)] = None,
) -> None:
    """Ends the session and clears the cookie; 204 when signed out already, too."""
    token = auth.token_in(session_cookie)
    if token is not None:
        auth.end_session(session, token)
    response.delete_cookie(auth.COOKIE, path="/", secure=True, httponly=True, samesite="lax")


@router.get("/me")
def me(user: CurrentUser, session: DbSession) -> MeOut:
    return _me_out(users.me(session, user.org_id, user.id))
