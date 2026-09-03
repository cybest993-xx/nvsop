"""The seam `auth`'s use cases reach persistence through.

`Protocol`s rather than base classes: the PostgreSQL adapter arrives in this module's
`adapters/`, and the use-case tests pass in-memory stand-ins at this same seam (harness §4).
Neither the protocols nor their callers import an adapter, which an `import-linter` contract
holds.

No method commits. One request is one transaction, opened and committed by the HTTP adapter
layer (ADR-0002), so a repository that committed would break the property the ADR exists for:
that a use case touching two modules cannot half-succeed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from factory_sop.auth.model import Session, User


class UserRepository(Protocol):
    """`auth_user`. Administration of accounts — create, edit, deactivate — arrives with #23."""

    def add(self, user: User) -> None:
        """Insert an account. Used by the bootstrap command and, from #23, the administration
        use cases.

        Raises on a login name that is already taken: that is a real unique constraint rather
        than a check the caller makes first, so two concurrent creations cannot both succeed.
        """
        ...

    def has_any(self) -> bool:
        """Whether the store holds any account at all.

        The bootstrap's precondition, not a listing: a deployment creates its first account
        exactly once, and the question is one bit.
        """
        ...

    def by_login_name(self, login_name: str) -> User | None:
        """Return the account whose login name is exactly `login_name`, if there is one."""
        ...

    def by_identifier(self, user_id: UUID) -> User | None:
        """Return the account `user_id` names, if it still exists."""
        ...


class SessionRepository(Protocol):
    """`auth_session`: the live logins."""

    def add(self, session: Session) -> None:
        """Persist a newly opened session."""
        ...

    def by_token_fingerprint(self, token_fingerprint: str) -> Session | None:
        """Return the session that fingerprint identifies, live or not.

        Liveness is `SessionPolicy`'s decision, not a filter here: the store holds the two
        instants, and the policy that turns them into an expiry can change between requests.
        """
        ...

    def touch(self, session: Session, *, last_used_at: datetime) -> Session | None:
        """Slide `session`'s idle window forward and return the updated record.

        `None` when the row is already gone: another request revoked it between this one's
        read and its slide. The caller refuses rather than proceeding on a session that no
        longer exists.
        """
        ...

    def remove(self, session: Session) -> None:
        """Delete one session. This is what logging out is."""
        ...

    def remove_every_session_of(self, user_id: UUID) -> int:
        """Delete all of an account's sessions, returning how many there were.

        Deactivating a user must revoke every session it has (§5.15), and that happens in
        the same transaction as the deactivation.
        """
        ...
