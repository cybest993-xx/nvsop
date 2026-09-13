"""`auth`'s tables: `auth_user`, `auth_session`, `auth_role`, `auth_role_permission`,
`auth_user_role` and the `auth_permission` registry (§七).

The `auth_` prefix is not decoration — it is what makes migration ownership statically
decidable, and `scripts/check_migration_ownership.py` fails a migration that touches a table
whose prefix names a different module.

Mapped classes are separate from the domain types in `auth/model.py` rather than being them.
The domain types are frozen dataclasses with no persistence machinery, which is what lets the
use cases be tested without a database; a single class doing both jobs would put SQLAlchemy's
instrumentation inside the rules about who may log in. The translation is in this file, in one
place per table.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from factory_sop.auth.model import Role, Session, User, UserStatus
from factory_sop.auth.permissions import Permission
from factory_sop.persistence import Table

# A hex SHA-256 digest (`auth/tokens.py`). Fixed width, so a value of another shape means the
# code writing it changed.
TOKEN_FINGERPRINT_LENGTH = 64


class BootstrapGuardRow(Table):
    """The database claim that makes first-account creation single-winner."""

    __tablename__ = "auth_bootstrap_guard"
    __table_args__ = (CheckConstraint("singleton = 1", name="singleton_is_one"),)

    # Every bootstrap attempts the same primary key. PostgreSQL serializes concurrent inserts;
    # the winner creates the account in that transaction and every later attempt skips.
    singleton: Mapped[int] = mapped_column(Integer(), primary_key=True)


class UserRow(Table):
    """A local account."""

    __tablename__ = "auth_user"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    # Unique, and the natural key an import or an administrator matches on — never the URL
    # identity, which is the UUIDv7 above (§5.15).
    login_name: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(128))
    # The Argon2id PHC encoding, which carries its own salt and cost parameters. Long enough
    # for those parameters to be raised without a migration.
    password_hash: Mapped[str] = mapped_column(String(255))
    status: Mapped[UserStatus] = mapped_column(
        Enum(
            UserStatus,
            name="auth_user_status",
            values_callable=lambda enum: [m.value for m in enum],
        )
    )
    # Who created the account and who last changed it. Plain UUIDs rather than a self foreign
    # key: this is attribution for the 变更归属 rule (§5.15), not integrity — the trail of what
    # changed is the diagnostic log's (Q37), and no query needs a join to answer "who did this".
    # Nullable because a database that predates the columns has rows with no one to name.
    created_by: Mapped[UUID | None] = mapped_column(Uuid())
    updated_by: Mapped[UUID | None] = mapped_column(Uuid())

    def to_domain(self) -> User:
        return User(
            id=self.id,
            login_name=self.login_name,
            display_name=self.display_name,
            password_hash=self.password_hash,
            status=self.status,
            created_by=self.created_by,
            updated_by=self.updated_by,
        )

    @classmethod
    def from_domain(cls, user: User) -> UserRow:
        return cls(
            id=user.id,
            login_name=user.login_name,
            display_name=user.display_name,
            password_hash=user.password_hash,
            status=user.status,
            created_by=user.created_by,
            updated_by=user.updated_by,
        )


class SessionRow(Table):
    """One live login. Deleting the row is what revoking the session is (§六)."""

    __tablename__ = "auth_session"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    # `ondelete="CASCADE"`, so deleting an account cannot leave a session that authenticates
    # requests as a user who is gone. Deactivation is the reversible path and revokes sessions
    # through the use case; this covers the irreversible one (§5.15: 删除 is a separate
    # permitted operation).
    user_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("auth_user.id", ondelete="CASCADE"), index=True
    )
    # Not the token: `auth/tokens.py` explains why the table holds only its digest. Unique,
    # because two sessions sharing one would make the lookup ambiguous.
    token_fingerprint: Mapped[str] = mapped_column(String(TOKEN_FINGERPRINT_LENGTH), unique=True)
    # `timezone=True` on both: §5.15 fixes UTC timestamps, and a `TIMESTAMP WITHOUT TIME ZONE`
    # reads back naive, which would make every lifetime comparison in `SessionPolicy` raise.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def to_domain(self) -> Session:
        return Session(
            id=self.id,
            user_id=self.user_id,
            token_fingerprint=self.token_fingerprint,
            created_at=self.created_at,
            last_used_at=self.last_used_at,
        )

    @classmethod
    def from_domain(cls, session: Session) -> SessionRow:
        return cls(
            id=session.id,
            user_id=session.user_id,
            token_fingerprint=session.token_fingerprint,
            created_at=session.created_at,
            last_used_at=session.last_used_at,
        )


class PermissionRow(Table):
    """The registry of registered permissions (§七).

    One row per member of `auth/permissions.py`'s enum, seeded by the migration that introduces
    the member: a module's own `auth`-prefixed migration inserts its codes here when the module
    lands. `auth_role_permission` carries a foreign key to this table, so a permission no
    module registered cannot be stored — the second gate behind `parse_permission` at the seam.

    Registry rows are never removed in the run of a change that only adds permissions; taking
    one away belongs to the removing module's own migration, and the repository reads past a
    code its build no longer registers rather than failing every holder's request.
    """

    __tablename__ = "auth_permission"

    # The `module.resource.action` value itself. 64 is comfortably above the longest plausible
    # triple, and the string is the wire format — no mapping between table and wire to keep true.
    code: Mapped[str] = mapped_column(String(64), primary_key=True)


class RoleRow(Table):
    """A role. Its permissions are rows in `auth_role_permission`, not a column here."""

    __tablename__ = "auth_role"

    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    # Unique, and the key a fixture or an operator's script names a role by — never the URL
    # identity, which is the UUIDv7 above (§5.15).
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    # Same attribution pair `auth_user` carries, for the same rule (§5.15).
    created_by: Mapped[UUID | None] = mapped_column(Uuid())
    updated_by: Mapped[UUID | None] = mapped_column(Uuid())

    def to_domain(self, *, permissions: frozenset[Permission]) -> Role:
        return Role(
            id=self.id,
            code=self.code,
            name=self.name,
            permissions=permissions,
            created_by=self.created_by,
            updated_by=self.updated_by,
        )


class RolePermissionRow(Table):
    """One permission granted by one role.

    A row per permission rather than an array column or a comma-joined string. Two reasons, and
    both are about the queries this module actually runs: resolving a caller's permission set is
    a join, and the last-administration guard asks "which active accounts hold this permission",
    which is a `WHERE` on an indexed column here. Against an array or a delimited string, both
    become a scan with parsing in the middle.

    `permission` is a plain string column, not a database enum, but a foreign key to
    `auth_permission`: this set grows by one member every time a module is added, and a database
    enum would make each of those an `ALTER TYPE` migration in `auth` for a value `auth` does
    not own. Membership is enforced where it is decidable — `parse_permission` on the way in,
    the registry's key on the way down.
    """

    __tablename__ = "auth_role_permission"

    role_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("auth_role.id", ondelete="CASCADE"), primary_key=True
    )
    # The registry's code, and the reason an unregistered permission cannot be stored.
    permission: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("auth_permission.code", ondelete="RESTRICT"),
        primary_key=True,
        index=True,
    )


class UserRoleRow(Table):
    """One role held by one account. The composite primary key is what makes it a set.

    Both sides cascade: deleting an account or a role must not leave an assignment naming
    something that is gone, and an assignment row carries no information of its own to preserve.
    """

    __tablename__ = "auth_user_role"

    user_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("auth_user.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[UUID] = mapped_column(
        Uuid(), ForeignKey("auth_role.id", ondelete="CASCADE"), primary_key=True, index=True
    )
