"""CSRF protection for the session cookie: a token derived from the session, not stored.

§六 requires a `HttpOnly + Secure + SameSite` session cookie **and** a CSRF check on every
modifying request. The cookie alone is nearly enough — `SameSite=Strict` means a browser does
not send it on a cross-site request at all — but "nearly" rests on the browser's behavior
being correct and on no future route relaxing the attribute, so the second check is here as
well.

The shape is a double-submit: this token goes out in a cookie the page's JavaScript **can**
read, and comes back in a request header. A cross-origin page can do neither — it cannot read
another origin's cookie, and it cannot set a header on a form submission.

It is an HMAC of the session token rather than an independent random value, which buys two
things a random value does not. It is bound to one session, so a token from one login cannot
be replayed against another; and it needs no storage, so `auth_session` has no column for it
and the domain model stays free of a purely transport-level concern.
"""

from __future__ import annotations

import hashlib
import hmac

from factory_sop.auth.tokens import SessionToken


def csrf_token_for(session: SessionToken, *, secret: str) -> str:
    """Derive the CSRF token that belongs to `session`."""
    return hmac.new(
        secret.encode("utf-8"), session.value.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_csrf_token(presented: str | None, *, session: SessionToken, secret: str) -> bool:
    """Report whether `presented` is the CSRF token derived from `session`.

    A missing token is a failure rather than an omission to tolerate: that is exactly what a
    cross-site request looks like.
    """
    if presented is None:
        return False
    # Constant-time, so the comparison does not leak how many leading characters were right.
    return hmac.compare_digest(presented, csrf_token_for(session, secret=secret))
