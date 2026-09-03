"""In-memory stand-ins for `auth`'s two repositories.

They sit at the same seam the PostgreSQL adapters do (harness §4: replace the adapter at the
seam, do not mock through the call chain). What they are for is the use-case tests: session
lifetime, refusal, and what a login rejects are decidable without a database, and putting
them behind one would make the suite that proves the safety rules the slow one.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from uuid import UUID

from factory_sop.auth.model import Session, User, UserStatus
from factory_sop.auth.passwords import hash_password
from factory_sop.identifiers import new_id


@dataclass
class FakeUsers:
    """A `UserRepository` over a dict."""

    by_id: dict[UUID, User] = field(default_factory=dict)

    def register(
        self,
        *,
        login_name: str,
        password: str,
        status: UserStatus = UserStatus.ACTIVE,
    ) -> User:
        """Build an account from a plaintext password, store it, and return it.

        Test set-up rather than part of the protocol: the protocol's `add` takes an already
        built `User`, and every use-case test wants "an account whose password is this".
        """
        user = User(
            id=new_id(),
            login_name=login_name,
            display_name=login_name.title(),
            password_hash=hash_password(password),
            status=status,
        )
        self.add(user)
        return user

    def add(self, user: User) -> None:
        if any(
            stored.login_name == user.login_name and stored.id != user.id
            for stored in self.by_id.values()
        ):
            # The real adapter has a unique constraint doing this (§5.15). A fake that let a
            # duplicate through would make a test pass against behavior PostgreSQL refuses.
            raise ValueError(f"login name already taken: {user.login_name}")
        self.by_id[user.id] = user

    def deactivate(self, user_id: UUID) -> User:
        """Deactivate an account (`CONTEXT.md`: 停用). Test set-up; #23 owns the use case."""
        deactivated = replace(self.by_id[user_id], status=UserStatus.DEACTIVATED)
        self.by_id[user_id] = deactivated
        return deactivated

    def by_login_name(self, login_name: str) -> User | None:
        return next(
            (user for user in self.by_id.values() if user.login_name == login_name),
            None,
        )

    def by_identifier(self, user_id: UUID) -> User | None:
        return self.by_id.get(user_id)


@dataclass
class FakeSessions:
    """A `SessionRepository` over a dict keyed by token fingerprint."""

    by_fingerprint: dict[str, Session] = field(default_factory=dict)

    def add(self, session: Session) -> None:
        self.by_fingerprint[session.token_fingerprint] = session

    def by_token_fingerprint(self, token_fingerprint: str) -> Session | None:
        return self.by_fingerprint.get(token_fingerprint)

    def touch(self, session: Session, *, last_used_at: datetime) -> Session | None:
        if session.token_fingerprint not in self.by_fingerprint:
            # Same contract as the real adapter: a row that is already gone reads as None, so
            # a use-case test can drive the concurrent-revocation refusal without a database.
            return None
        refreshed = replace(session, last_used_at=last_used_at)
        self.by_fingerprint[refreshed.token_fingerprint] = refreshed
        return refreshed

    def remove(self, session: Session) -> None:
        self.by_fingerprint.pop(session.token_fingerprint, None)

    def remove_every_session_of(self, user_id: UUID) -> int:
        doomed = [
            fingerprint
            for fingerprint, session in self.by_fingerprint.items()
            if session.user_id == user_id
        ]
        for fingerprint in doomed:
            del self.by_fingerprint[fingerprint]
        return len(doomed)
