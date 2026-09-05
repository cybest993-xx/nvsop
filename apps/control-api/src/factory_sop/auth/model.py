"""What `auth` owns in this slice: the account, the session, and how long one stays usable.

Pure domain types — no SQLAlchemy, no FastAPI, no clock. Every instant arrives from a caller
holding one, so a lifetime rule is decidable by passing two datetimes rather than by waiting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID


class UserStatus(StrEnum):
    """Whether an account may still take part in a new login.

    An enum rather than an `is_active` boolean: `CONTEXT.md` gives 停用 its own definition
    across every mutable configuration object, and the call site reads
    `status is UserStatus.DEACTIVATED` instead of `not user.is_active`.
    """

    ACTIVE = "active"
    DEACTIVATED = "deactivated"


@dataclass(frozen=True, slots=True)
class User:
    """A local account. Roles and permissions attach to it in #23."""

    id: UUID
    login_name: str
    display_name: str
    password_hash: str
    status: UserStatus


@dataclass(frozen=True, slots=True)
class Session:
    """One live login, as `auth_session` holds it.

    It stores the two instants a lifetime is computed **from**, not the instant it expires
    at. Two reasons, and the first is the one that matters: an operator raising the idle
    timeout expects it to apply to the sessions that are open, and a stored `expires_at`
    would leave every existing row on the old policy until each was next written. The
    second is that a stored expiry can disagree with the configured policy, and then
    neither is the answer.

    Forcing a session offline is therefore a delete rather than a write of an earlier
    expiry — which is what §六 asks of a session store anyway: revocable immediately.
    """

    id: UUID
    user_id: UUID
    token_fingerprint: str
    created_at: datetime
    last_used_at: datetime


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    """How long a session survives: idle, and in total.

    Two limits rather than one, because they answer different questions. The idle timeout is
    what closes an unattended browser on the shop floor; the absolute lifetime is what
    guarantees a stolen token stops working eventually even if it is used continuously.
    """

    idle_timeout: timedelta
    absolute_lifetime: timedelta

    def expires_at(self, session: Session) -> datetime:
        """Return the instant `session` stops being usable: whichever limit comes first."""
        return min(
            session.last_used_at + self.idle_timeout,
            session.created_at + self.absolute_lifetime,
        )

    def is_live(self, session: Session, *, now: datetime) -> bool:
        """Report whether `session` may still authenticate a request at `now`."""
        return now < self.expires_at(session)
