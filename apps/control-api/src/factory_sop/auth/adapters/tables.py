"""`auth`'s tables: `auth_user` and `auth_session` (§七).

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

from factory_sop.auth.model import Session, User, UserStatus
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

    def to_domain(self) -> User:
        return User(
            id=self.id,
            login_name=self.login_name,
            display_name=self.display_name,
            password_hash=self.password_hash,
            status=self.status,
        )

    @classmethod
    def from_domain(cls, user: User) -> UserRow:
        return cls(
            id=user.id,
            login_name=user.login_name,
            display_name=user.display_name,
            password_hash=user.password_hash,
            status=user.status,
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
