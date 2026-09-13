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

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from factory_sop.auth.model import Role, Session, User
from factory_sop.auth.permissions import Permission


class LoginNameTakenError(Exception):
    """The database rejected an account because its natural login key already exists."""

    def __init__(self, login_name: str) -> None:
        super().__init__(f"login name already taken: {login_name}")
        self.login_name = login_name


class RoleCodeTakenError(Exception):
    """The database rejected a role because its natural code already exists."""

    def __init__(self, code: str) -> None:
        super().__init__(f"role code already taken: {code}")
        self.code = code


class UserNotFoundError(Exception):
    """A write targeted an account that disappeared after it was read."""

    def __init__(self, user_id: UUID) -> None:
        super().__init__(f"user not found: {user_id}")
        self.user_id = user_id


class RoleNotFoundError(Exception):
    """A write targeted a role that disappeared after it was read."""

    def __init__(self, role_id: UUID) -> None:
        super().__init__(f"role not found: {role_id}")
        self.role_id = role_id


class UserRepository(Protocol):
    """`auth_user`. Administration of accounts — create, edit, deactivate — arrives with #23."""

    def add(self, user: User) -> None:
        """Insert an account. Used by the bootstrap command and, from #23, the administration
        use cases.

        Raises on a login name that is already taken: that is a real unique constraint rather
        than a check the caller makes first, so two concurrent creations cannot both succeed.
        """
        ...

    def claim_bootstrap(self) -> bool:
        """Atomically claim the right to create the deployment's first account.

        Exactly one concurrent bootstrap transaction receives ``True``. Once any account or
        a successful claim exists, every later call receives ``False``.
        """
        ...

    def by_login_name(self, login_name: str) -> User | None:
        """Return the account whose login name is exactly `login_name`, if there is one."""
        ...

    def by_identifier(self, user_id: UUID) -> User | None:
        """Return the account `user_id` names, if it still exists."""
        ...

    def update(self, user: User) -> None:
        """Write an already-modified account back.

        The domain types are frozen, so an edit is `dataclasses.replace` followed by this — the
        use case never mutates a `User` in place, and what reaches persistence is a whole
        consistent record rather than a field at a time. Raises `UserNotFoundError` when the row
        disappeared after the use case read it.
        """
        ...

    def remove(self, user_id: UUID) -> None:
        """Delete an account outright (§5.15: 删除, opened by its own permission).

        Its sessions and role assignments go with it, by `ON DELETE CASCADE` in the adapter: a
        surviving session would authenticate requests as a user who is gone. Raises
        `UserNotFoundError` when the row disappeared after the use case read it.
        """
        ...

    def page(self, *, page: int, page_size: int) -> tuple[Sequence[User], int]:
        """Return one ordered account page and the total number of accounts.

        Deactivated accounts are included because 恢复 is reachable only from this listing. The
        adapter applies the limit in the database, so a large staff list does not become an
        unbounded response or memory allocation in the HTTP layer.
        """
        ...

    def every(self) -> Sequence[User]:
        """Every account, deactivated ones included, ordered by login name.

        Kept for adapter-level inspection and small in-memory fixtures; paginated callers use
        `page` so production listings do not load the whole table.
        """
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


class RoleRepository(Protocol):
    """`auth_role`, `auth_role_permission` and `auth_user_role`.

    One repository over the three, not three: a role's permission set and who holds it are never
    read or written apart from the role, and splitting them would let a caller write half
    a role. The permission rows are a role's contents; the assignment rows are what makes a
    permission reach a request.
    """

    def add(self, role: Role) -> None:
        """Insert a role and its permission set. Raises on a code that is already taken.

        The refusal is the real unique constraint rather than a check the caller makes first,
        so two concurrent creations cannot both succeed.
        """
        ...

    def update(self, role: Role) -> None:
        """Overwrite a role's name and permission set.

        The set is replaced, not merged: the screen is a list of checkboxes, and a merge would
        make unticking one do nothing. Raises `RoleNotFoundError` when the row disappeared after
        the use case read it.
        """
        ...

    def remove(self, role_id: UUID) -> None:
        """Delete a role, and with it every assignment of it.

        Raises `RoleNotFoundError` when the row disappeared after the use case read it.
        """
        ...

    def by_identifier(self, role_id: UUID) -> Role | None:
        """Return the role `role_id` names, if it still exists."""
        ...

    def by_code(self, code: str) -> Role | None:
        """Return the role whose code is exactly `code`, if there is one."""
        ...

    def page(self, *, page: int, page_size: int) -> tuple[Sequence[Role], int]:
        """Return one ordered role page and the total number of roles from the database."""
        ...

    def every(self) -> Sequence[Role]:
        """Every role, ordered by code, for adapter-level inspection and small fixtures."""
        ...

    def assign(self, *, user_id: UUID, role_ids: Iterable[UUID]) -> None:
        """Replace an account's roles with exactly `role_ids`.

        Replace rather than add, matching the screen: the administrator submits the set the
        account should have, and an empty one means it holds none.
        """
        ...

    def role_ids_of(self, user_id: UUID) -> frozenset[UUID]:
        """The roles an account holds. What the edit screen preselects."""
        ...

    def permissions_of(self, user_id: UUID) -> frozenset[Permission]:
        """The union of every permission the account's roles grant.

        This is what becomes `Caller.granted`, resolved once per request. The union is computed
        here rather than by the caller so the adapter can do it in one query across the three
        tables instead of returning roles for the caller to fold.
        """
        ...

    def active_assignees_of(self, role_id: UUID) -> frozenset[UUID]:
        """Which active accounts hold this role.

        Paired with the method below to answer "would this permission survive an edit that keeps
        it in the role": the holders are unchanged, so the question is whether the role has any.
        """
        ...

    def active_holders_of(
        self, permission: Permission, *, ignoring_role: UUID | None = None
    ) -> frozenset[UUID]:
        """Which active accounts hold `permission`, optionally as if one role did not exist.

        The last-administration guard's one question. `ignoring_role` is what makes it answerable
        before the write rather than after: "who would still hold this if that role were deleted,
        or if its permissions were replaced" is asked by excluding the role and consulting the
        rest.

        Active accounts only. A deactivated holder cannot log in, so it is not an administrator
        who could undo a lockout — counting it would permit deactivating two administrators in
        turn, and the second one would be the lockout.
        """
        ...

    def acquire_administration_lock(self) -> None:
        """Serialize the check-then-write of every operation that can reduce who may administer.

        The last-administration guard reads who holds a permission and refuses when the operation
        would take the last holder away. Read, decide, write is one transaction — but two
        transactions deciding against the same pre-state can both pass an unlocked read and both
        commit, which is exactly the permanent lockout the guard exists to prevent. Everything
        that can take a grant away therefore takes this transaction-scoped lock first, so the
        second operation's read happens after the first one's write, and refuses.

        It is held to the end of the caller's transaction, which is the HTTP adapter layer's
        (ADR-0002). These operations are rare administrative actions, so serialization costs
        nothing the deployment would notice.
        """
        ...
