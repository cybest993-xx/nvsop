"""In-memory stand-ins for `auth`'s two repositories.

They sit at the same seam the PostgreSQL adapters do (harness §4: replace the adapter at the
seam, do not mock through the call chain). What they are for is the use-case tests: session
lifetime, refusal, and what a login rejects are decidable without a database, and putting
them behind one would make the suite that proves the safety rules the slow one.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from uuid import UUID

from factory_sop.auth.authorization import Caller
from factory_sop.auth.model import Role, Session, User, UserStatus
from factory_sop.auth.passwords import hash_password
from factory_sop.auth.permissions import Permission
from factory_sop.auth.repository import (
    LoginNameTakenError,
    RoleCodeTakenError,
    RoleNotFoundError,
    UserNotFoundError,
)
from factory_sop.identifiers import new_id


def caller_holding(*granted: Permission, user: User | None = None) -> Caller:
    """A `Caller` with exactly these permissions, for a use case that requires one.

    Test set-up, not a fixture of the domain: a use case takes its `Caller` as an argument
    (`auth/authorization.py`), and almost every administration test needs one holding a single
    permission. Passing `user` is for the cases that assert on *who* acted — the
    last-administrator guard, and the `actor_id` on a diagnostic line.
    """
    return Caller(
        user=user
        or User(
            id=new_id(),
            login_name="administrator",
            display_name="系统管理员",
            # A placeholder, not a credential: nothing verifies against it.
            password_hash="argon2-encoded",  # pragma: allowlist secret
            status=UserStatus.ACTIVE,
        ),
        granted=frozenset(granted),
    )


@dataclass
class FakeUsers:
    """A `UserRepository` over a dict."""

    by_id: dict[UUID, User] = field(default_factory=dict)
    bootstrap_claimed: bool = False

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
            raise LoginNameTakenError(user.login_name)
        self.by_id[user.id] = user

    def claim_bootstrap(self) -> bool:
        if self.bootstrap_claimed or self.by_id:
            return False
        self.bootstrap_claimed = True
        return True

    def deactivate(self, user_id: UUID) -> User:
        """Deactivate an account (`CONTEXT.md`: 停用) without going through the use case.

        Set-up for the session suites, which need a deactivated account and are not about how it
        got that way. The use case is `usecases/users.deactivate_user`, and it also revokes the
        sessions — which is why this one is not used by the administration suite.
        """
        deactivated = replace(self.by_id[user_id], status=UserStatus.DEACTIVATED)
        self.by_id[user_id] = deactivated
        return deactivated

    def update(self, user: User) -> None:
        if user.id not in self.by_id:
            raise UserNotFoundError(user.id)
        self.by_id[user.id] = user

    def remove(self, user_id: UUID) -> None:
        if user_id not in self.by_id:
            raise UserNotFoundError(user_id)
        self.by_id.pop(user_id)

    def every(self) -> Sequence[User]:
        # Ordered here because the protocol says the store orders it: a test asserting the
        # listing's order against a fake that returned insertion order would prove nothing about
        # the adapter.
        return sorted(self.by_id.values(), key=lambda user: user.login_name)

    def page(self, *, page: int, page_size: int) -> tuple[Sequence[User], int]:
        ordered = self.every()
        start = (page - 1) * page_size
        return ordered[start : start + page_size], len(ordered)

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


@dataclass
class FakeRoles:
    """A `RoleRepository` over two dicts: the roles, and who holds which.

    It takes the accounts as well, because `active_holders_of` must not count a deactivated
    holder — the real adapter joins `auth_user` in one query, and a fake that ignored status
    would let the last-administrator tests pass against behavior PostgreSQL does not have. A
    suite that is not about status leaves it unset, and then every holder counts as active.
    """

    by_id: dict[UUID, Role] = field(default_factory=dict)
    assignments: dict[UUID, frozenset[UUID]] = field(default_factory=dict)
    # The accounts, so `active_holders_of` can exclude deactivated holders. The real adapter joins
    # `auth_user` in one query; this is the same information reached the same way.
    users: FakeUsers | None = None

    def grant(self, user: User | None, *permissions: Permission) -> Role | None:
        """Give `user` a role carrying exactly these permissions, and return it.

        Test set-up rather than part of the protocol: what a suite usually wants is "this account
        may do these things", and building a role and an assignment for each case would bury the
        assertion. Returns `None` for a `None` user so a caller can pass a repository lookup
        straight in.
        """
        if user is None:
            return None
        role = Role(
            id=new_id(),
            code=f"granted_{len(self.by_id)}",
            name="测试角色",
            permissions=frozenset(permissions),
        )
        self.add(role)
        self.assign(user_id=user.id, role_ids=[*self.role_ids_of(user.id), role.id])
        return role

    def add(self, role: Role) -> None:
        if any(stored.code == role.code and stored.id != role.id for stored in self.by_id.values()):
            # The real adapter has a unique constraint doing this (§5.15). A fake that let a
            # duplicate through would make a test pass against behavior PostgreSQL refuses.
            raise RoleCodeTakenError(role.code)
        self.by_id[role.id] = role

    def update(self, role: Role) -> None:
        if role.id not in self.by_id:
            raise RoleNotFoundError(role.id)
        self.by_id[role.id] = role

    def remove(self, role_id: UUID) -> None:
        if role_id not in self.by_id:
            raise RoleNotFoundError(role_id)
        self.by_id.pop(role_id)
        # The assignment rows go with it, as `ON DELETE CASCADE` does in the adapter.
        self.assignments = {user_id: held - {role_id} for user_id, held in self.assignments.items()}

    def by_identifier(self, role_id: UUID) -> Role | None:
        return self.by_id.get(role_id)

    def by_code(self, code: str) -> Role | None:
        return next((role for role in self.by_id.values() if role.code == code), None)

    def every(self) -> Sequence[Role]:
        # Ordered here because the protocol says the store orders it: a test asserting the
        # listing's order against a fake that returned insertion order would prove nothing about
        # the adapter.
        return sorted(self.by_id.values(), key=lambda role: role.code)

    def page(self, *, page: int, page_size: int) -> tuple[Sequence[Role], int]:
        ordered = self.every()
        start = (page - 1) * page_size
        return ordered[start : start + page_size], len(ordered)

    def assign(self, *, user_id: UUID, role_ids: Iterable[UUID]) -> None:
        self.assignments[user_id] = frozenset(role_ids)

    def role_ids_of(self, user_id: UUID) -> frozenset[UUID]:
        return self.assignments.get(user_id, frozenset())

    def permissions_of(self, user_id: UUID) -> frozenset[Permission]:
        granted: set[Permission] = set()
        for role_id in self.role_ids_of(user_id):
            role = self.by_id.get(role_id)
            if role is not None:
                granted |= role.permissions
        return frozenset(granted)

    def active_assignees_of(self, role_id: UUID) -> frozenset[UUID]:
        return frozenset(
            user_id
            for user_id, held in self.assignments.items()
            if role_id in held and self._is_active(user_id)
        )

    def active_holders_of(
        self, permission: Permission, *, ignoring_role: UUID | None = None
    ) -> frozenset[UUID]:
        holders: set[UUID] = set()
        for user_id, held in self.assignments.items():
            if not self._is_active(user_id):
                continue
            for role_id in held:
                if role_id == ignoring_role:
                    continue
                role = self.by_id.get(role_id)
                if role is not None and permission in role.permissions:
                    holders.add(user_id)
                    break
        return frozenset(holders)

    def acquire_administration_lock(self) -> None:
        # A dict has no transactions to serialize; the real adapter takes a PostgreSQL advisory
        # lock here, and the concurrency that makes it necessary is what the integration suite
        # against real PostgreSQL proves.
        return None

    def _is_active(self, user_id: UUID) -> bool:
        """Whether the account is in service.

        With no `users` attached, every holder counts as active. That is the safe direction for a
        test that is not about status: the guard then refuses more readily, so a test asserting a
        refusal cannot pass for the wrong reason.
        """
        if self.users is None:
            return True
        user = self.users.by_identifier(user_id)
        return user is not None and user.status is UserStatus.ACTIVE
