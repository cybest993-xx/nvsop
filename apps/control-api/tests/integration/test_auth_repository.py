"""`auth`'s repositories against real PostgreSQL.

The use-case suite proves the rules with in-memory stand-ins; this proves the adapter that
has to behave the same way at that seam — that a session round-trips, that the unique
constraint on a login name is real, and that `remove_every_session_of` reaches exactly one
account's rows.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.auth.adapters.repository import (
    PostgresSessionRepository,
    PostgresUserRepository,
)
from factory_sop.auth.adapters.tables import UserRow
from factory_sop.auth.model import Session, User, UserStatus
from factory_sop.auth.passwords import hash_password
from factory_sop.identifiers import new_id

MONDAY_MORNING = datetime(2026, 9, 7, 1, 0, tzinfo=UTC)


def an_account(*, login_name: str = "wang.li", status: UserStatus = UserStatus.ACTIVE) -> User:
    return User(
        id=new_id(),
        login_name=login_name,
        display_name="王丽",
        password_hash=hash_password("assembly-line-3"),
        status=status,
    )


def a_session(user: User, *, fingerprint: str = "a" * 64) -> Session:
    return Session(
        id=new_id(),
        user_id=user.id,
        token_fingerprint=fingerprint,
        created_at=MONDAY_MORNING,
        last_used_at=MONDAY_MORNING,
    )


def test_an_account_round_trips_through_the_table(session: DatabaseSession) -> None:
    users = PostgresUserRepository(session)
    stored = an_account()
    users.add(stored)
    session.flush()
    session.expunge_all()

    assert users.by_login_name("wang.li") == stored
    assert users.by_identifier(stored.id) == stored


def test_a_deactivated_status_survives_the_round_trip(session: DatabaseSession) -> None:
    # The status is what refuses a login, so it has to come back as the enum member rather
    # than as whatever string the column holds.
    users = PostgresUserRepository(session)
    stored = an_account(status=UserStatus.DEACTIVATED)
    users.add(stored)
    session.flush()
    session.expunge_all()

    assert users.by_identifier(stored.id) == stored


def test_an_absent_account_reads_as_absent(session: DatabaseSession) -> None:
    users = PostgresUserRepository(session)

    assert users.by_login_name("nobody") is None
    assert users.by_identifier(new_id()) is None


def test_two_accounts_cannot_share_a_login_name(session: DatabaseSession) -> None:
    # The login name is a natural key with a real unique constraint (§5.15), not merely a
    # convention the application checks: two administrators creating the same account at once
    # must not produce two rows one password can open.
    users = PostgresUserRepository(session)
    users.add(an_account())

    with pytest.raises(IntegrityError):
        users.add(an_account())


def test_a_session_round_trips_and_is_found_by_its_fingerprint(session: DatabaseSession) -> None:
    users = PostgresUserRepository(session)
    sessions = PostgresSessionRepository(session)
    owner = an_account()
    users.add(owner)
    stored = a_session(owner)
    sessions.add(stored)
    session.flush()
    session.expunge_all()

    assert sessions.by_token_fingerprint(stored.token_fingerprint) == stored


def test_the_stored_instants_come_back_as_utc(session: DatabaseSession) -> None:
    # §5.15: timestamps are UTC RFC3339. A naive datetime read back would make every lifetime
    # comparison in `SessionPolicy` raise, and only at runtime.
    users = PostgresUserRepository(session)
    sessions = PostgresSessionRepository(session)
    owner = an_account()
    users.add(owner)
    sessions.add(a_session(owner))
    session.flush()
    session.expunge_all()

    read = sessions.by_token_fingerprint("a" * 64)

    assert read is not None
    assert read.created_at == MONDAY_MORNING
    assert read.created_at.tzinfo is not None


def test_touching_a_session_persists_the_new_instant(session: DatabaseSession) -> None:
    users = PostgresUserRepository(session)
    sessions = PostgresSessionRepository(session)
    owner = an_account()
    users.add(owner)
    stored = a_session(owner)
    sessions.add(stored)
    session.flush()
    used_at = MONDAY_MORNING + timedelta(hours=5)

    touched = sessions.touch(stored, last_used_at=used_at)
    session.flush()
    session.expunge_all()

    assert touched is not None
    assert touched.last_used_at == used_at
    assert sessions.by_token_fingerprint(stored.token_fingerprint) == touched


def test_touching_a_session_whose_row_a_concurrent_revoke_removed_reports_the_loss(
    session: DatabaseSession,
) -> None:
    # Logging out in one tab while another tab's request is in flight: the row this request
    # read is gone by the time it slides. The None is what the use case refuses on — handing
    # back the unwritten record would let the request proceed authenticated on a session that
    # no longer exists.
    users = PostgresUserRepository(session)
    sessions = PostgresSessionRepository(session)
    owner = an_account()
    users.add(owner)
    stored = a_session(owner)
    sessions.add(stored)
    session.flush()

    sessions.remove(stored)
    session.flush()
    session.expunge_all()

    assert sessions.touch(stored, last_used_at=MONDAY_MORNING + timedelta(hours=5)) is None


def test_a_touch_and_concurrent_logout_have_one_serial_order_without_a_stale_write(
    engine: Engine,
) -> None:
    # Request A slides the session, then request B starts logout before A commits. `touch` must
    # issue its write immediately: PostgreSQL then serializes B after A instead of letting B
    # delete first and making A's deferred ORM update explode as StaleDataError at commit.
    owner = an_account(login_name="race.operator")
    stored = a_session(owner, fingerprint="9" * 64)
    setup = DatabaseSession(engine)
    reader = DatabaseSession(engine)
    try:
        PostgresUserRepository(setup).add(owner)
        PostgresSessionRepository(setup).add(stored)
        setup.commit()

        reader_sessions = PostgresSessionRepository(reader)
        restored = reader_sessions.by_token_fingerprint(stored.token_fingerprint)
        assert restored == stored
        touched = reader_sessions.touch(
            restored,
            last_used_at=MONDAY_MORNING + timedelta(hours=5),
        )
        assert touched is not None

        def log_out() -> None:
            with DatabaseSession(engine) as revoker:
                sessions = PostgresSessionRepository(revoker)
                concurrent = sessions.by_token_fingerprint(stored.token_fingerprint)
                assert concurrent == stored
                sessions.remove(concurrent)
                revoker.commit()

        with ThreadPoolExecutor(max_workers=1) as executor:
            logout = executor.submit(log_out)
            # A correct touch holds the row lock, so logout is still waiting. The broken
            # deferred update lets logout finish here and A then fails on commit.
            with suppress(FutureTimeoutError):
                logout.result(timeout=0.5)
            reader.commit()
            logout.result(timeout=5)
    finally:
        setup.close()
        reader.close()
        with engine.begin() as connection:
            connection.execute(delete(UserRow).where(UserRow.id == owner.id))


def test_removing_a_session_removes_only_that_one(session: DatabaseSession) -> None:
    users = PostgresUserRepository(session)
    sessions = PostgresSessionRepository(session)
    owner = an_account()
    users.add(owner)
    doomed = a_session(owner, fingerprint="b" * 64)
    kept = a_session(owner, fingerprint="c" * 64)
    sessions.add(doomed)
    sessions.add(kept)
    session.flush()

    sessions.remove(doomed)
    session.flush()

    assert sessions.by_token_fingerprint("b" * 64) is None
    assert sessions.by_token_fingerprint("c" * 64) == kept


def test_revoking_an_account_s_sessions_leaves_another_account_untouched(
    session: DatabaseSession,
) -> None:
    # What deactivating a user runs (§5.15). Reaching one row too many would log out an
    # unrelated operator; one too few would leave a deactivated account working.
    users = PostgresUserRepository(session)
    sessions = PostgresSessionRepository(session)
    deactivated = an_account(login_name="wang.li")
    bystander = an_account(login_name="zhang.wei")
    users.add(deactivated)
    users.add(bystander)
    sessions.add(a_session(deactivated, fingerprint="d" * 64))
    sessions.add(a_session(deactivated, fingerprint="e" * 64))
    sessions.add(a_session(bystander, fingerprint="f" * 64))
    session.flush()

    revoked = sessions.remove_every_session_of(deactivated.id)
    session.flush()

    assert revoked == 2
    assert sessions.by_token_fingerprint("d" * 64) is None
    assert sessions.by_token_fingerprint("e" * 64) is None
    assert sessions.by_token_fingerprint("f" * 64) is not None


def test_revoking_the_sessions_of_an_account_that_has_none_is_not_an_error(
    session: DatabaseSession,
) -> None:
    sessions = PostgresSessionRepository(session)

    assert sessions.remove_every_session_of(new_id()) == 0


def test_a_session_cannot_reference_an_account_that_does_not_exist(
    session: DatabaseSession,
) -> None:
    # The foreign key is what makes "every session belongs to an account" a fact rather than
    # an expectation, and it is why `restore_session` treats a missing user as a bug it
    # refuses rather than a state it handles.
    sessions = PostgresSessionRepository(session)
    sessions.add(a_session(an_account()))

    with pytest.raises(IntegrityError):
        session.flush()
