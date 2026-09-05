"""The PostgreSQL side of `auth`'s repository seams.

No method commits. One request is one transaction, opened and committed by the HTTP adapter
layer (ADR-0002); a repository that committed would break exactly the property that ADR
exists for — that a use case spanning two modules cannot half-succeed. So these methods flush
where a later read in the same request depends on the write being visible, and nothing more.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, exists, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DatabaseSession
from sqlalchemy.orm.exc import StaleDataError

from factory_sop.auth.adapters.tables import (
    BootstrapGuardRow,
    RolePermissionRow,
    RoleRow,
    SessionRow,
    UserRoleRow,
    UserRow,
)
from factory_sop.auth.model import Role, Session, User, UserStatus
from factory_sop.auth.permissions import Permission, UnregisteredPermissionError, parse_permission
from factory_sop.auth.repository import (
    LoginNameTakenError,
    RoleCodeTakenError,
    RoleNotFoundError,
    UserNotFoundError,
)

# The advisory-lock key `acquire_administration_lock` takes. One fixed number for the whole
# backend: what it serializes is the last-administration guard's check-then-write, and two
# keys would let two guards run against the same pre-state. Transaction-scoped, so a rollback
# releases it and a request that never reaches the write holds nothing.
ADMINISTRATION_LOCK_KEY = 0x41554C  # "AUL" — administration lock, stable across processes.


class PostgresUserRepository:
    """`auth_user` through the request's session."""

    def __init__(self, session: DatabaseSession) -> None:
        self._session = session

    def add(self, user: User) -> None:
        """Insert an account. Flushed rather than left for the transaction's end, because a
        session inserted later in the same transaction carries a foreign key to this row —
        SQLAlchemy orders inserts across two mappers only when a `relationship` connects them,
        and these tables have none: the domain types are plain frozen dataclasses, and adding a
        relationship the module never reads in order to hint at flush order would put an unused
        mapping in the adapter. Still no commit — the transaction is the HTTP layer's (ADR-0002),
        or the bootstrap command's, which owns its own.
        """
        self._session.add(UserRow.from_domain(user))
        try:
            self._session.flush()
        except IntegrityError as error:
            if _violates_constraint(error, "uq_auth_user_login_name"):
                raise LoginNameTakenError(user.login_name) from error
            raise

    def claim_bootstrap(self) -> bool:
        # Every process inserts the same primary key. PostgreSQL blocks concurrent attempts
        # until the winner commits, then `ON CONFLICT` makes every loser return no row. The
        # claim and the account share one transaction, so a failed creation releases the claim.
        claimed = self._session.scalar(
            insert(BootstrapGuardRow)
            .values(singleton=1)
            .on_conflict_do_nothing(index_elements=[BootstrapGuardRow.singleton])
            .returning(BootstrapGuardRow.singleton)
        )
        if claimed is None:
            return False
        # Handles a database upgraded from before the guard existed, or an account created by
        # the administration path before bootstrap ever ran. Keep the guard and skip forever.
        return not cast(
            "bool",
            self._session.scalar(select(exists().select_from(UserRow))),
        )

    def by_login_name(self, login_name: str) -> User | None:
        row = self._session.scalar(select(UserRow).where(UserRow.login_name == login_name))
        return row.to_domain() if row is not None else None

    def by_identifier(self, user_id: UUID) -> User | None:
        row = self._session.get(UserRow, user_id)
        return row.to_domain() if row is not None else None

    def update(self, user: User) -> None:
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(UserRow)
                .where(UserRow.id == user.id)
                .values(
                    display_name=user.display_name,
                    password_hash=user.password_hash,
                    status=user.status,
                    updated_by=user.updated_by,
                )
            ),
        )
        if result.rowcount == 0:
            # A direct UPDATE avoids trusting a stale ORM identity-map row after another request
            # deleted the account. The use case turns this into the stable 404 refusal path.
            raise UserNotFoundError(user.id)

    def remove(self, user_id: UUID) -> None:
        result = cast(
            "CursorResult[Any]",
            self._session.execute(delete(UserRow).where(UserRow.id == user_id)),
        )
        if result.rowcount == 0:
            raise UserNotFoundError(user_id)

    def page(self, *, page: int, page_size: int) -> tuple[Sequence[User], int]:
        total = int(self._session.scalar(select(func.count()).select_from(UserRow)) or 0)
        rows = self._session.scalars(
            select(UserRow)
            .order_by(UserRow.login_name)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return [row.to_domain() for row in rows], total

    def every(self) -> Sequence[User]:
        rows = self._session.scalars(select(UserRow).order_by(UserRow.login_name)).all()
        return [row.to_domain() for row in rows]


class PostgresRoleRepository:
    """`auth_role`, `auth_role_permission` and `auth_user_role` through the request's session.

    One adapter over the three tables, matching the one `RoleRepository` seam: a role's
    permissions and its assignments are never read or written apart from the role, and three
    repositories would let a caller write half a role.
    """

    def __init__(self, session: DatabaseSession) -> None:
        self._session = session

    def add(self, role: Role) -> None:
        self._session.add(
            RoleRow(
                id=role.id,
                code=role.code,
                name=role.name,
                created_by=role.created_by,
                updated_by=role.updated_by,
            )
        )
        try:
            self._session.flush()
        except IntegrityError as error:
            if _violates_constraint(error, "uq_auth_role_code"):
                raise RoleCodeTakenError(role.code) from error
            raise
        # Flushed **before** the permission rows, not after both. They carry a foreign key to this
        # role, and SQLAlchemy orders inserts across two mappers only when a `relationship` connects
        # them — which these deliberately have none of (see `adapters/tables.py`). Without the
        # flush above the permission rows can reach PostgreSQL ahead of the role and be refused.
        self._write_permissions(role)
        self._session.flush()

    def update(self, role: Role) -> None:
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(RoleRow)
                .where(RoleRow.id == role.id)
                .values(name=role.name, updated_by=role.updated_by)
            ),
        )
        if result.rowcount == 0:
            # A direct UPDATE avoids trusting a stale ORM identity-map row after another request
            # deleted the role. The use case turns this into the stable 404 refusal path.
            raise RoleNotFoundError(role.id)
        # Deleted and rewritten rather than diffed. The set is small, the statement is one
        # `DELETE` plus one `INSERT` per member, and a diff would be code whose only purpose is to
        # avoid rows PostgreSQL rewrites anyway.
        self._session.execute(delete(RolePermissionRow).where(RolePermissionRow.role_id == role.id))
        self._write_permissions(role)
        self._session.flush()

    def remove(self, role_id: UUID) -> None:
        result = cast(
            "CursorResult[Any]",
            self._session.execute(delete(RoleRow).where(RoleRow.id == role_id)),
        )
        if result.rowcount == 0:
            raise RoleNotFoundError(role_id)

    def by_identifier(self, role_id: UUID) -> Role | None:
        row = self._session.get(RoleRow, role_id)
        return self._to_domain(row) if row is not None else None

    def by_code(self, code: str) -> Role | None:
        row = self._session.scalar(select(RoleRow).where(RoleRow.code == code))
        return self._to_domain(row) if row is not None else None

    def page(self, *, page: int, page_size: int) -> tuple[Sequence[Role], int]:
        total = int(self._session.scalar(select(func.count()).select_from(RoleRow)) or 0)
        rows = self._session.scalars(
            select(RoleRow).order_by(RoleRow.code).offset((page - 1) * page_size).limit(page_size)
        ).all()
        return [self._to_domain(row) for row in rows], total

    def every(self) -> Sequence[Role]:
        rows = self._session.scalars(select(RoleRow).order_by(RoleRow.code)).all()
        return [self._to_domain(row) for row in rows]

    def assign(self, *, user_id: UUID, role_ids: Iterable[UUID]) -> None:
        # The delete is flushed by `execute` itself, so replacing a set with an overlapping one
        # cannot collide with the rows it is about to remove.
        self._session.execute(delete(UserRoleRow).where(UserRoleRow.user_id == user_id))
        for role_id in set(role_ids):
            self._session.add(UserRoleRow(user_id=user_id, role_id=role_id))
        self._session.flush()

    def role_ids_of(self, user_id: UUID) -> frozenset[UUID]:
        rows = self._session.scalars(
            select(UserRoleRow.role_id).where(UserRoleRow.user_id == user_id)
        ).all()
        return frozenset(rows)

    def permissions_of(self, user_id: UUID) -> frozenset[Permission]:
        # One query across the two tables rather than reading the roles and folding them here: this
        # runs on every authenticated request that reaches a use case needing a permission.
        rows = self._session.scalars(
            select(RolePermissionRow.permission)
            .join(UserRoleRow, UserRoleRow.role_id == RolePermissionRow.role_id)
            .where(UserRoleRow.user_id == user_id)
        ).all()
        return frozenset(self._known(rows))

    def active_assignees_of(self, role_id: UUID) -> frozenset[UUID]:
        rows = self._session.scalars(
            select(UserRoleRow.user_id)
            .join(UserRow, UserRow.id == UserRoleRow.user_id)
            .where(UserRoleRow.role_id == role_id)
            .where(UserRow.status == UserStatus.ACTIVE)
        ).all()
        return frozenset(rows)

    def active_holders_of(
        self, permission: Permission, *, ignoring_role: UUID | None = None
    ) -> frozenset[UUID]:
        query = (
            select(UserRoleRow.user_id)
            .join(
                RolePermissionRow,
                RolePermissionRow.role_id == UserRoleRow.role_id,
            )
            .join(UserRow, UserRow.id == UserRoleRow.user_id)
            .where(RolePermissionRow.permission == permission.value)
            .where(UserRow.status == UserStatus.ACTIVE)
        )
        if ignoring_role is not None:
            query = query.where(UserRoleRow.role_id != ignoring_role)
        return frozenset(self._session.scalars(query).all())

    def acquire_administration_lock(self) -> None:
        # `pg_advisory_xact_lock` needs no row to exist (unlike a `FOR UPDATE` on the guard row —
        # a fresh deployment has none), is released by commit or rollback on its own, and is
        # namespace-wide by key: this one constant is the queue every administration-reducing
        # operation lines up in.
        self._session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": ADMINISTRATION_LOCK_KEY}
        )

    def _write_permissions(self, role: Role) -> None:
        for permission in role.permissions:
            self._session.add(RolePermissionRow(role_id=role.id, permission=permission.value))

    def _to_domain(self, row: RoleRow) -> Role:
        stored = self._session.scalars(
            select(RolePermissionRow.permission).where(RolePermissionRow.role_id == row.id)
        ).all()
        return row.to_domain(permissions=frozenset(self._known(stored)))

    def _known(self, values: Iterable[str]) -> list[Permission]:
        """Parse stored strings, dropping any the current build no longer registers.

        Dropped rather than raised on, and this is the one place tolerance is right: a module
        removed from the product leaves its permission rows behind, and a role that still names
        one would otherwise make every request by every holder fail with a 500. What a dropped
        value means is that the permission grants nothing — which is exactly true, because
        nothing checks it any more. Removing the registry row belongs to that module's own
        removal migration.
        """
        known: list[Permission] = []
        for value in values:
            try:
                known.append(parse_permission(value))
            except UnregisteredPermissionError:
                continue
        return known


class PostgresSessionRepository:
    """`auth_session` through the request's session."""

    def __init__(self, session: DatabaseSession) -> None:
        self._session = session

    def add(self, session: Session) -> None:
        self._session.add(SessionRow.from_domain(session))

    def by_token_fingerprint(self, token_fingerprint: str) -> Session | None:
        row = self._session.scalar(
            select(SessionRow).where(SessionRow.token_fingerprint == token_fingerprint)
        )
        return row.to_domain() if row is not None else None

    def touch(self, session: Session, *, last_used_at: datetime) -> Session | None:
        row = self._session.get(SessionRow, session.id)
        if row is None:
            # The row was read moments ago in this same request and is now gone: a concurrent
            # revocation. The None is what tells the caller, so the request is refused instead
            # of proceeding authenticated on a session that no longer exists.
            return None
        row.last_used_at = last_used_at
        try:
            # Send the update now rather than at request commit. PostgreSQL takes the row lock
            # here, giving touch and logout one serial order; deferring it lets logout delete
            # first and turns this request into a StaleDataError after its handler returned.
            self._session.flush()
        except StaleDataError:
            # A delete that won the race between `get` and `flush` is the same unusable session
            # as a row absent at `get`. The request Unit of Work rolls the failed transaction
            # back when the use case raises SESSION_INVALID.
            return None
        return row.to_domain()

    def remove(self, session: Session) -> None:
        row = self._session.get(SessionRow, session.id)
        if row is not None:
            self._session.delete(row)

    def remove_every_session_of(self, user_id: UUID) -> int:
        # One statement rather than loading the rows: the caller wants them gone and the count,
        # and an account with many open browsers should not be read into memory to delete.
        # `CursorResult` is what a DML statement returns and is the only `Result` carrying
        # `rowcount`; `Session.execute` is typed as the general one.
        result = cast(
            "CursorResult[Any]",
            self._session.execute(delete(SessionRow).where(SessionRow.user_id == user_id)),
        )
        return result.rowcount


def _violates_constraint(error: IntegrityError, constraint: str) -> bool:
    """Identify one expected natural-key violation without translating other integrity errors."""
    diagnostic = getattr(error.orig, "diag", None)
    if getattr(diagnostic, "constraint_name", None) == constraint:
        return True
    # Drivers other than psycopg may not expose `diag`; their error text still names PostgreSQL's
    # constraint. This fallback keeps the domain seam stable without mapping unrelated failures.
    return constraint in str(error.orig)
