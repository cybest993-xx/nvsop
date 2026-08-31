from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from auth_fakes import FakeSessions, FakeUsers

from factory_sop.auth.errors import AuthenticationRefusedError, RefusalCode
from factory_sop.auth.model import Session, SessionPolicy, UserStatus
from factory_sop.auth.tokens import SessionToken, fingerprint
from factory_sop.auth.usecases.sessions import (
    open_session,
    restore_session,
    revoke_every_session_of,
    revoke_session,
)

POLICY = SessionPolicy(idle_timeout=timedelta(hours=12), absolute_lifetime=timedelta(days=30))
MONDAY_MORNING = datetime(2026, 9, 7, 1, 0, tzinfo=UTC)


def logged_in(
    users: FakeUsers,
    sessions: FakeSessions,
    *,
    login_name: str = "wang.li",
    password: str = "assembly-line-3",
    status: UserStatus = UserStatus.ACTIVE,
    now: datetime = MONDAY_MORNING,
) -> tuple[SessionToken, Session]:
    """Open a real session through the use case, and return its token and record."""
    users.register(login_name=login_name, password=password, status=status)
    opened = open_session(
        login_name=login_name,
        password=password,
        users=users,
        sessions=sessions,
        policy=POLICY,
        now=now,
    )
    return opened.token, opened.session


def test_a_token_from_a_previous_request_restores_the_same_session() -> None:
    # This is what a browser reopened after lunch does, and what every authenticated request
    # does: the cookie is the whole of the client's state, and the session lives here.
    users, sessions = FakeUsers(), FakeSessions()
    token, opened = logged_in(users, sessions)

    restored = restore_session(
        token=token,
        users=users,
        sessions=sessions,
        policy=POLICY,
        now=MONDAY_MORNING + timedelta(hours=4),
    )

    assert restored.user.login_name == "wang.li"
    assert restored.session.id == opened.id


def test_using_a_session_slides_its_idle_window_forward() -> None:
    # Without this, a shift longer than the idle timeout logs an operator out mid-task even
    # though they never stopped working.
    users, sessions = FakeUsers(), FakeSessions()
    token, _ = logged_in(users, sessions)
    used_at = MONDAY_MORNING + timedelta(hours=11)

    restored = restore_session(
        token=token, users=users, sessions=sessions, policy=POLICY, now=used_at
    )

    assert restored.session.last_used_at == used_at
    assert POLICY.expires_at(restored.session) == used_at + POLICY.idle_timeout


def test_the_slide_is_persisted_not_just_returned() -> None:
    users, sessions = FakeUsers(), FakeSessions()
    token, _ = logged_in(users, sessions)
    used_at = MONDAY_MORNING + timedelta(hours=11)

    restored = restore_session(
        token=token, users=users, sessions=sessions, policy=POLICY, now=used_at
    )

    assert sessions.by_token_fingerprint(fingerprint(token)) == restored.session


def test_a_session_left_idle_past_the_timeout_is_refused() -> None:
    users, sessions = FakeUsers(), FakeSessions()
    token, _ = logged_in(users, sessions)

    with pytest.raises(AuthenticationRefusedError) as refusal:
        restore_session(
            token=token,
            users=users,
            sessions=sessions,
            policy=POLICY,
            now=MONDAY_MORNING + POLICY.idle_timeout,
        )

    assert refusal.value.code == RefusalCode.SESSION_INVALID


def test_a_continuously_used_session_still_ends_at_its_absolute_lifetime() -> None:
    # The sliding window must not be able to extend a session forever: a token stolen from a
    # browser that is polling has to stop working eventually.
    users, sessions = FakeUsers(), FakeSessions()
    token, _ = logged_in(users, sessions)

    kept_alive = MONDAY_MORNING
    while kept_alive < MONDAY_MORNING + POLICY.absolute_lifetime - POLICY.idle_timeout:
        kept_alive += POLICY.idle_timeout - timedelta(minutes=1)
        restore_session(token=token, users=users, sessions=sessions, policy=POLICY, now=kept_alive)

    with pytest.raises(AuthenticationRefusedError) as refusal:
        restore_session(
            token=token,
            users=users,
            sessions=sessions,
            policy=POLICY,
            now=MONDAY_MORNING + POLICY.absolute_lifetime,
        )

    assert refusal.value.code == RefusalCode.SESSION_INVALID


def test_an_unknown_token_is_refused() -> None:
    users, sessions = FakeUsers(), FakeSessions()
    logged_in(users, sessions)

    with pytest.raises(AuthenticationRefusedError) as refusal:
        restore_session(
            token=SessionToken.issue(),
            users=users,
            sessions=sessions,
            policy=POLICY,
            now=MONDAY_MORNING,
        )

    assert refusal.value.code == RefusalCode.SESSION_INVALID


def test_a_session_whose_account_was_deactivated_stops_working() -> None:
    # Deactivation revokes every session in the same transaction (§5.15), so this is the
    # second line rather than the first: it is what holds if a session is opened concurrently
    # with the deactivation, or if some future writer forgets the revocation.
    users, sessions = FakeUsers(), FakeSessions()
    token, opened = logged_in(users, sessions)
    users.deactivate(opened.user_id)

    with pytest.raises(AuthenticationRefusedError) as refusal:
        restore_session(
            token=token, users=users, sessions=sessions, policy=POLICY, now=MONDAY_MORNING
        )

    assert refusal.value.code == RefusalCode.SESSION_INVALID


def test_a_refused_session_is_removed_rather_than_left_to_be_retried() -> None:
    users, sessions = FakeUsers(), FakeSessions()
    token, _ = logged_in(users, sessions)

    with pytest.raises(AuthenticationRefusedError):
        restore_session(
            token=token,
            users=users,
            sessions=sessions,
            policy=POLICY,
            now=MONDAY_MORNING + POLICY.idle_timeout,
        )

    assert sessions.by_token_fingerprint(fingerprint(token)) is None


def test_logging_out_revokes_the_session_it_was_asked_about() -> None:
    users, sessions = FakeUsers(), FakeSessions()
    token, _ = logged_in(users, sessions)

    revoke_session(token=token, sessions=sessions)

    assert sessions.by_fingerprint == {}


def test_logging_out_twice_is_not_an_error() -> None:
    # The browser dropped the cookie the first time. A second call — a retry, a second tab —
    # has nothing to do and no reason to fail.
    users, sessions = FakeUsers(), FakeSessions()
    token, _ = logged_in(users, sessions)

    revoke_session(token=token, sessions=sessions)
    revoke_session(token=token, sessions=sessions)

    assert sessions.by_fingerprint == {}


def test_logging_out_leaves_the_account_s_other_sessions_alone() -> None:
    users, sessions = FakeUsers(), FakeSessions()
    token, _ = logged_in(users, sessions)
    other = open_session(
        login_name="wang.li",
        password="assembly-line-3",
        users=users,
        sessions=sessions,
        policy=POLICY,
        now=MONDAY_MORNING,
    )

    revoke_session(token=token, sessions=sessions)

    assert sessions.by_token_fingerprint(fingerprint(other.token)) == other.session


def test_revoking_an_account_s_sessions_ends_all_of_them() -> None:
    # What deactivating a user calls (§5.15). It is here rather than in #23 because it is a
    # session-lifetime rule; the deactivation use case that invokes it arrives with #23.
    users, sessions = FakeUsers(), FakeSessions()
    _, opened = logged_in(users, sessions)
    open_session(
        login_name="wang.li",
        password="assembly-line-3",
        users=users,
        sessions=sessions,
        policy=POLICY,
        now=MONDAY_MORNING,
    )

    revoked = revoke_every_session_of(user_id=opened.user_id, sessions=sessions)

    assert revoked == 2
    assert sessions.by_fingerprint == {}
