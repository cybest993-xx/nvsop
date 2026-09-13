"""What `auth` owns in this slice: the account, the session, and how long one stays usable.

Pure domain types — no SQLAlchemy, no FastAPI, no clock. Every instant arrives from a caller
holding one, so a lifetime rule is decidable by passing two datetimes rather than by waiting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from factory_sop.auth.permissions import Permission


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
    """A local account. Its roles are held by `RoleRepository`, not by this type.

    Deliberately not carrying a `roles` list: a `User` is read on every authenticated request,
    and the login path needs the credential and the status without the two extra joins that
    resolving roles costs. What a request needs the roles *for* is the permission set, and that
    is resolved once per request into `Caller.granted` (`auth/authorization.py`).
    """

    id: UUID
    login_name: str
    display_name: str
    password_hash: str
    status: UserStatus
    # Who created the account and who last changed it (§5.15 carries 变更归属 in these columns;
    # the *trail* of changes is the diagnostic log's job, Q37). `None` only on a row written by
    # an older build; every path in this module sets them.
    created_by: UUID | None = None
    updated_by: UUID | None = None


@dataclass(frozen=True, slots=True)
class Role:
    """A named set of permissions (Q5/Q14). Nothing else — no hierarchy, no inheritance.

    A user may hold several, and what they may do is the union. Keeping a role a plain set is
    what makes the screen honest: the checkboxes an administrator ticks are the permissions that
    are enforced, with no rule in between that could grant a seventh thing they did not tick.

    `permissions` holds `Permission` members rather than strings, so a value that is not
    registered cannot be in a role at all — the parse happens at the edge, in the use case that
    writes one, and everything below this point is a closed set.
    """

    id: UUID
    # The stable identifier a fixture, an import or an operator's script names a role by, and
    # what the last-administration guard reports. Unique; never the URL identity, which is the
    # UUIDv7 above (§5.15).
    code: str
    name: str
    permissions: frozenset[Permission]
    created_by: UUID | None = None
    updated_by: UUID | None = None


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
