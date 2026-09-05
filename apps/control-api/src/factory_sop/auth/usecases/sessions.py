"""Opening, restoring and revoking a session: how a caller acquires and drops an identity.

No permission is checked in this file. These are the use cases that run before the caller has
an identity to check a permission against — the password is the authorization for opening one,
and holding the token is the authorization for ending it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn
from uuid import UUID

from factory_sop.auth.errors import AuthenticationRefusedError, RefusalCode
from factory_sop.auth.model import Session, SessionPolicy, User, UserStatus
from factory_sop.auth.passwords import hash_password, verify_password
from factory_sop.auth.repository import SessionRepository, UserRepository
from factory_sop.auth.tokens import SessionToken, fingerprint
from factory_sop.identifiers import new_id
from factory_sop.observability import get_logger

_logger = get_logger("auth")

# Verified against when no account matches the login name, so a request for an account that
# does not exist costs the same Argon2 work as one for an account that does. Without it, the
# response time tells an unauthenticated caller which login names are real. The value is
# never a valid password for it: `hash_password` salts per call, so nothing hashes to this.
_ABSENT_ACCOUNT_HASH = hash_password("no account has this password")


@dataclass(frozen=True, slots=True)
class OpenedSession:
    """A session that now exists, and the one copy of its token.

    The token is returned rather than stored: `sessions` holds only its fingerprint, so this
    is the only moment the value exists anywhere but the browser.
    """

    session: Session
    user: User
    token: SessionToken


def open_session(
    *,
    login_name: str,
    password: str,
    users: UserRepository,
    sessions: SessionRepository,
    policy: SessionPolicy,
    now: datetime,
) -> OpenedSession:
    """Authenticate a local account and open a session for it.

    Raises `AuthenticationRefusedError` with `CREDENTIALS_REJECTED` when the pair does not
    identify an account, and with `ACCOUNT_DEACTIVATED` when it identifies a deactivated one.
    """
    user = users.by_login_name(login_name)
    # The password is verified even when there is no account, and before the status is
    # consulted: the work has to happen on every path for the timing not to be a signal, and
    # `ACCOUNT_DEACTIVATED` may only be reported to someone who proved they hold the
    # credential.
    correct = verify_password(
        password=password,
        stored_hash=user.password_hash if user is not None else _ABSENT_ACCOUNT_HASH,
    )
    if user is None or not correct:
        _refuse(RefusalCode.CREDENTIALS_REJECTED, login_name=login_name)
    if user.status is UserStatus.DEACTIVATED:
        _refuse(RefusalCode.ACCOUNT_DEACTIVATED, login_name=login_name)

    token = SessionToken.issue()
    session = Session(
        id=new_id(),
        user_id=user.id,
        token_fingerprint=fingerprint(token),
        created_at=now,
        last_used_at=now,
    )
    sessions.add(session)
    _logger.info(
        "auth.session.opened",
        user_id=str(user.id),
        session_id=str(session.id),
        expires_at=policy.expires_at(session).isoformat(),
    )
    return OpenedSession(session=session, user=user, token=token)


@dataclass(frozen=True, slots=True)
class RestoredSession:
    """A session that authenticated the current request, with its idle window slid forward."""

    session: Session
    user: User


def restore_session(
    *,
    token: SessionToken,
    users: UserRepository,
    sessions: SessionRepository,
    policy: SessionPolicy,
    now: datetime,
) -> RestoredSession:
    """Authenticate a request carrying `token`, and keep the session alive by using it.

    This runs on every authenticated request, and is also what "restore my session" is: a
    browser reopened with the cookie still in it presents the same token, and there is no
    separate resume path to keep working.

    Raises `AuthenticationRefusedError` with `SESSION_INVALID` when the token identifies no usable
    session — unknown, expired, revoked, or belonging to an account since deactivated. The
    row is removed on the way out, so a token that has been refused once cannot be retried
    against a store that still holds it.
    """
    session = sessions.by_token_fingerprint(fingerprint(token))
    if session is None:
        _refuse_session(reason="unknown_token")

    user = users.by_identifier(session.user_id)
    if (
        user is None
        or user.status is UserStatus.DEACTIVATED
        or not policy.is_live(session, now=now)
    ):
        # Deleted rather than left in place. An expired row is otherwise re-read on every
        # subsequent request, and a session belonging to a deactivated account would sit
        # there indefinitely being refused one request at a time.
        sessions.remove(session)
        _refuse_session(reason=_refusal_reason(user=user, session=session, policy=policy, now=now))

    touched = sessions.touch(session, last_used_at=now)
    if touched is None:
        # The row was revoked by another request after this one read it. Same code as any
        # other unusable session: the client discards the cookie and logs in again.
        _refuse_session(reason="revoked_during_request")

    return RestoredSession(session=touched, user=user)


def revoke_session(*, token: SessionToken, sessions: SessionRepository) -> None:
    """End the session `token` names. This is what logging out is.

    Idempotent: a token naming no session leaves nothing to do. A second call is a retry or a
    second tab, not an error, and reporting one would give an unauthenticated caller a way to
    ask whether a token is live.
    """
    session = sessions.by_token_fingerprint(fingerprint(token))
    if session is None:
        return
    sessions.remove(session)
    _logger.info(
        "auth.session.revoked",
        user_id=str(session.user_id),
        session_id=str(session.id),
    )


def revoke_every_session_of(*, user_id: UUID, sessions: SessionRepository) -> int:
    """End every session of one account, returning how many there were.

    Deactivating a user must revoke all of its sessions (§5.15), in the same transaction as
    the deactivation — otherwise the account is refused at the login form while its open
    browsers keep working.
    """
    revoked = sessions.remove_every_session_of(user_id)
    _logger.info("auth.session.revoked_all", user_id=str(user_id), revoked=revoked)
    return revoked


def _refusal_reason(
    *, user: User | None, session: Session, policy: SessionPolicy, now: datetime
) -> str:
    """Name the cause for the diagnostic line. Not reported to the client (§5.15)."""
    if user is None:
        return "user_missing"
    if user.status is UserStatus.DEACTIVATED:
        return "user_deactivated"
    if now >= session.created_at + policy.absolute_lifetime:
        return "absolute_lifetime_reached"
    return "idle_timeout_reached"


def _refuse_session(*, reason: str) -> NoReturn:
    """Log why the session was refused and raise the one code the client is told.

    The client gets `SESSION_INVALID` for all of these, because its behavior is the same for
    each and the states are not reliably distinguishable from outside. An administrator
    investigating "it logged me out" needs the distinction, so it lives in the diagnostic
    line rather than in the response.
    """
    _logger.info(
        "auth.session.refused",
        error_code=RefusalCode.SESSION_INVALID.value,
        reason=reason,
    )
    raise AuthenticationRefusedError(RefusalCode.SESSION_INVALID)


def _refuse(code: RefusalCode, *, login_name: str) -> NoReturn:
    """Log the refusal and raise it.

    `login_name` is in the line and the password is not, in any form — not the value, not a
    length, not a hash. It is the natural key an administrator investigating a lockout
    searches by, and §5.15's rule is that diagnostics carry no credential.
    """
    _logger.info("auth.session.refused", error_code=code.value, login_name=login_name)
    raise AuthenticationRefusedError(code)
