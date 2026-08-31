"""The two cookies a login sets, and what makes them safe to set.

§六 fixes the shape: `HttpOnly + Secure + SameSite` for the session, and a CSRF check on every
modifying request. That is two cookies with deliberately different rules — the session one the
page must **not** be able to read, and the CSRF one it must.
"""

from __future__ import annotations

from typing import Literal

from fastapi import Response

from factory_sop.auth.csrf import csrf_token_for
from factory_sop.auth.tokens import SessionToken
from factory_sop.settings import Settings

SESSION_COOKIE = "sop_session"
CSRF_COOKIE = "sop_csrf"

# What the page echoes the CSRF cookie back in. A header, because a cross-origin form
# submission cannot set one — that asymmetry is the whole of the protection.
CSRF_HEADER = "x-csrf-token"

# `strict`, not `lax`. `lax` still sends the cookie on a top-level navigation, which is enough
# for a link in an e-mail to arrive authenticated. Nothing here needs to be reachable that
# way: the Web application is served from the same origin as the API (§六). Annotated with the
# literal type Starlette accepts, so widening it to a plain `str` fails the type check rather
# than reaching the browser as an ignored attribute.
SAME_SITE: Literal["strict"] = "strict"


def attach_session(
    response: Response, *, token: SessionToken, settings: Settings, max_age_seconds: int
) -> None:
    """Set both cookies for a session that has just been opened."""
    secure = settings.session_cookie_transport == "require_https"
    response.set_cookie(
        SESSION_COOKIE,
        token.value,
        # The page never reads this one. An XSS then cannot exfiltrate the session, which is
        # the reason §六 forbids keeping a long-lived token in `localStorage`.
        httponly=True,
        secure=secure,
        samesite=SAME_SITE,
        # Bounded by the session's own absolute lifetime, so the browser never keeps a cookie
        # that could not possibly still work. The server remains the authority: the row is
        # what decides, and deleting it revokes the session immediately (§六).
        max_age=max_age_seconds,
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token_for(token, secret=settings.csrf_secret.get_secret_value()),
        # Readable on purpose: the page's JavaScript has to copy it into `CSRF_HEADER`. It is
        # not a credential — on its own it authenticates nothing, and it is derived from the
        # session token rather than revealing it (`auth/csrf.py`).
        httponly=False,
        secure=secure,
        samesite=SAME_SITE,
        max_age=max_age_seconds,
        path="/",
    )


def clear_session(response: Response, *, settings: Settings) -> None:
    """Remove both cookies. Paired with revoking the session server-side, never alone."""
    secure = settings.session_cookie_transport == "require_https"
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        # The attributes have to match the ones the cookie was set with, or the browser treats
        # this as a different cookie and leaves the original in place.
        response.delete_cookie(name, secure=secure, samesite=SAME_SITE, path="/")
