"""Real PostgreSQL races at the natural-key constraints.

The use cases perform a fast lookup for a useful error message, but that lookup is not a
concurrency guarantee. These tests hold one insert open while a second transaction attempts the
same key, then commit the winner and assert the adapter translates PostgreSQL's unique violation
at the repository seam. Unit tests cover the use-case mapping from that seam to the stable
problem code.
"""

from __future__ import annotations

from threading import Event, Thread
from uuid import uuid4

from sqlalchemy import Engine, text
from sqlalchemy.orm import sessionmaker

from factory_sop.auth.adapters.repository import (
    PostgresRoleRepository,
    PostgresUserRepository,
)
from factory_sop.auth.model import Role, User, UserStatus
from factory_sop.auth.passwords import hash_password
from factory_sop.auth.repository import LoginNameTakenError, RoleCodeTakenError
from factory_sop.identifiers import new_id

PASSWORD = "assembly-line-3"  # pragma: allowlist secret


def an_account(login_name: str) -> User:
    return User(
        id=new_id(),
        login_name=login_name,
        display_name=login_name,
        password_hash=hash_password(PASSWORD),
        status=UserStatus.ACTIVE,
    )


def test_concurrent_user_creation_translates_the_unique_login_conflict(engine: Engine) -> None:
    login_name = f"race-{uuid4().hex}"
    winner = an_account(login_name)
    loser = an_account(login_name)
    connection_a = engine.connect()
    transaction_a = connection_a.begin()
    session_a = sessionmaker(bind=connection_a)()
    PostgresUserRepository(session_a).add(winner)

    started = Event()
    errors: list[BaseException] = []

    def contend() -> None:
        connection_b = engine.connect()
        transaction_b = connection_b.begin()
        session_b = sessionmaker(bind=connection_b)()
        try:
            session_b.execute(text("SELECT 1")).scalar_one()
            started.set()
            PostgresUserRepository(session_b).add(loser)
        except BaseException as error:  # asserted below; do not let a thread hide a failure
            errors.append(error)
        finally:
            session_b.close()
            if transaction_b.is_active:
                transaction_b.rollback()
            connection_b.close()

    contender = Thread(target=contend)
    contender.start()
    assert started.wait(timeout=5)
    transaction_a.commit()
    contender.join(timeout=5)
    assert not contender.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], LoginNameTakenError)

    session_a.close()
    connection_a.close()
    with engine.begin() as cleanup:
        cleanup.execute(text("DELETE FROM auth_user WHERE id = :id"), {"id": winner.id})


def test_concurrent_role_creation_translates_the_unique_code_conflict(engine: Engine) -> None:
    code = f"race-{uuid4().hex}"
    winner = Role(id=new_id(), code=code, name="赢家", permissions=frozenset())
    loser = Role(id=new_id(), code=code, name="竞争者", permissions=frozenset())
    connection_a = engine.connect()
    transaction_a = connection_a.begin()
    session_a = sessionmaker(bind=connection_a)()
    PostgresRoleRepository(session_a).add(winner)

    started = Event()
    errors: list[BaseException] = []

    def contend() -> None:
        connection_b = engine.connect()
        transaction_b = connection_b.begin()
        session_b = sessionmaker(bind=connection_b)()
        try:
            session_b.execute(text("SELECT 1")).scalar_one()
            started.set()
            PostgresRoleRepository(session_b).add(loser)
        except BaseException as error:  # asserted below; do not let a thread hide a failure
            errors.append(error)
        finally:
            session_b.close()
            if transaction_b.is_active:
                transaction_b.rollback()
            connection_b.close()

    contender = Thread(target=contend)
    contender.start()
    assert started.wait(timeout=5)
    transaction_a.commit()
    contender.join(timeout=5)
    assert not contender.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], RoleCodeTakenError)

    session_a.close()
    connection_a.close()
    with engine.begin() as cleanup:
        cleanup.execute(text("DELETE FROM auth_role WHERE id = :id"), {"id": winner.id})
