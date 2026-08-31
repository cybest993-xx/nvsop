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

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.auth.adapters.tables import SessionRow, UserRow
from factory_sop.auth.model import Session, User


class PostgresUserRepository:
    """`auth_user` through the request's session."""

    def __init__(self, session: DatabaseSession) -> None:
        self._session = session

    def add(self, user: User) -> None:
        """Insert an account. Used by the bootstrap entrypoint; #23 adds administration.

        Flushed rather than left for the transaction's end, because a session inserted later
        in the same transaction carries a foreign key to this row. SQLAlchemy orders inserts
        across two mappers only when a `relationship` connects them, and these tables have
        none: the domain types are plain frozen dataclasses, and adding a relationship the
        module never reads in order to hint at flush order would put an unused mapping in the
        adapter. Still no commit — the transaction is the HTTP layer's (ADR-0002).
        """
        self._session.add(UserRow.from_domain(user))
        self._session.flush()

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

    def touch(self, session: Session, *, last_used_at: datetime) -> Session:
        row = self._session.get(SessionRow, session.id)
        if row is None:
            # The row was read moments ago in this same request and is now gone: a concurrent
            # revocation. Returning the unwritten value would let the request proceed on a
            # session that no longer exists, so the caller is told the slide did not happen.
            return session
        row.last_used_at = last_used_at
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
