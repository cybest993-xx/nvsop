"""The mapped table registry and the engine: shared infrastructure, like `observability`.

One `MetaData` for the whole backend, because the migration history is a single linear one
(§六). Ownership is not weakened by sharing it: a table belongs to the module whose package
declares it and whose prefix its name carries, and `scripts/check_migration_ownership.py`
decides that statically from those two facts. What the shared registry buys is that Alembic
can compare the whole schema in one pass, so a table a module forgot to migrate is a failure
rather than an absence.

This package owns no domain behavior and imports no domain module. Each module's tables live
in its own `adapters/`, above this.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy import Engine, MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from factory_sop.settings import Settings

# Constraint and index names are generated from one convention rather than left to PostgreSQL,
# so a migration that drops a constraint can name it without reading it out of the database
# first — `alembic autogenerate` writes the name it computes here, and it is the same name
# whichever version of PostgreSQL created it.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Table(DeclarativeBase):
    """The declarative base every mapped table derives from."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def database_url(settings: Settings) -> str:
    """Build the psycopg3 URL from resolved settings.

    The password is read out of its `SecretStr` here, at the one place a connection is opened.
    `SecretStr` renders as `**********` everywhere else, which is what keeps a credential out
    of a traceback or a log line (§5.12).
    """
    return (
        f"postgresql+psycopg://{settings.database_user}:"
        f"{settings.database_password.get_secret_value()}@"
        f"{settings.database_host}:{settings.database_port}/{settings.database_name}"
    )


def create_database_engine(settings: Settings) -> Engine:
    """Open the connection pool. Once per process, at the entrypoint."""
    return create_engine(database_url(settings), pool_pre_ping=True)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return the factory the request-scoped Unit of Work draws its session from (ADR-0002).

    `expire_on_commit` stays on: an object read after its transaction committed would
    otherwise be serialized from values that are no longer known to be current.
    """
    return sessionmaker(bind=engine)


def request_session(request: Request) -> Iterator[Session]:
    """The request's one transaction: opened here, committed here, never by a module.

    ADR-0002's Unit of Work. Every use case a request touches shares this session, so a
    request that spans two modules commits both or neither — which is why the ADR requires the
    mechanical boundary checks: the transaction no longer backs up the module boundary.

    Rolled back if the handler raises, including on a refusal that becomes a 4xx. A refusal is
    a request that did not happen, and the alternative — committing what ran before the
    refusal — would leave a half-applied operation behind an error response.
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    else:
        dispatcher = getattr(request.app.state, "job_dispatcher", None)
        job_ids = session.info.pop("job_dispatch_ids", set())
        if dispatcher is not None and isinstance(job_ids, set):
            for job_id in job_ids:
                if isinstance(job_id, UUID):
                    dispatcher.dispatch(job_id)
    finally:
        session.close()


# ADR-0002：所有适配器共享此依赖，提交失败必须发生在成功响应与 Cookie 发出前。
RequestSession = Annotated[Session, Depends(request_session, scope="function")]
