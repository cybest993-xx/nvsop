"""The PostgreSQL side of `auth`'s two repository seams.

No method commits. One request is one transaction, opened and committed by the HTTP adapter
layer (ADR-0002); a repository that committed would break exactly the property that ADR
exists for — that a use case spanning two modules cannot half-succeed. So these methods flush
where a later read in the same request depends on the write being visible, and nothing more.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, exists, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as DatabaseSession
from sqlalchemy.orm.exc import StaleDataError

from factory_sop.auth.adapters.tables import BootstrapGuardRow, SessionRow, UserRow
from factory_sop.auth.model import Session, User


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
        self._session.flush()

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
