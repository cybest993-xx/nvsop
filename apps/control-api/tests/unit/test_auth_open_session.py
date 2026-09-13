from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from auth_fakes import FakeSessions, FakeUsers

from factory_sop.auth.errors import AuthenticationRefusedError, RefusalCode
from factory_sop.auth.model import Session, SessionPolicy, UserStatus
from factory_sop.auth.tokens import fingerprint
from factory_sop.auth.usecases.sessions import open_session

POLICY = SessionPolicy(idle_timeout=timedelta(hours=12), absolute_lifetime=timedelta(days=30))
MONDAY_MORNING = datetime(2026, 9, 7, 1, 0, tzinfo=UTC)


def test_a_local_account_opens_a_session_with_its_own_password() -> None:
    users = FakeUsers()
    sessions = FakeSessions()
    operator = users.register(
        login_name="wang.li",
        password="assembly-line-3",  # pragma: allowlist secret
    )

    opened = open_session(
        login_name="wang.li",
        password="assembly-line-3",  # pragma: allowlist secret
        users=users,
        sessions=sessions,
        policy=POLICY,
        now=MONDAY_MORNING,
    )

    assert opened.user == operator
    assert opened.session == Session(
        id=opened.session.id,
        user_id=operator.id,
        token_fingerprint=fingerprint(opened.token),
        created_at=MONDAY_MORNING,
        last_used_at=MONDAY_MORNING,
    )


def test_the_opened_session_is_the_one_the_returned_token_finds() -> None:
    # The token is the caller's only handle on the session it just opened; the fingerprint
    # is what was persisted. This is the join between them.
    users = FakeUsers()
    sessions = FakeSessions()
    users.register(login_name="wang.li", password="assembly-line-3")  # pragma: allowlist secret

    opened = open_session(
        login_name="wang.li",
        password="assembly-line-3",  # pragma: allowlist secret
        users=users,
        sessions=sessions,
        policy=POLICY,
        now=MONDAY_MORNING,
    )

    assert sessions.by_token_fingerprint(fingerprint(opened.token)) == opened.session


def test_a_wrong_password_opens_nothing() -> None:
    users = FakeUsers()
    sessions = FakeSessions()
    users.register(login_name="wang.li", password="assembly-line-3")  # pragma: allowlist secret

    with pytest.raises(AuthenticationRefusedError) as refusal:
        open_session(
            login_name="wang.li",
            password="assembly-line-4",  # pragma: allowlist secret
            users=users,
            sessions=sessions,
            policy=POLICY,
            now=MONDAY_MORNING,
        )

    assert refusal.value.code == RefusalCode.CREDENTIALS_REJECTED
    assert sessions.by_fingerprint == {}


def test_an_unknown_login_name_is_refused_as_the_same_rejection() -> None:
    # Same code as a wrong password: telling the two apart would turn the login form into a
    # way to enumerate who has an account here.
    users = FakeUsers()

    with pytest.raises(AuthenticationRefusedError) as refusal:
        open_session(
            login_name="nobody",
            password="assembly-line-3",  # pragma: allowlist secret
            users=users,
            sessions=FakeSessions(),
            policy=POLICY,
            now=MONDAY_MORNING,
        )

    assert refusal.value.code == RefusalCode.CREDENTIALS_REJECTED


def test_a_deactivated_account_cannot_open_a_session_with_the_right_password() -> None:
    # `CONTEXT.md`: a deactivated object no longer takes part in new logins, and that is
    # reversible rather than a deletion — so the account, its password and its history are
    # all still here. The refusal is the whole of what deactivation means for `auth`.
    users = FakeUsers()
    sessions = FakeSessions()
    users.register(
        login_name="wang.li",
        password="assembly-line-3",  # pragma: allowlist secret
        status=UserStatus.DEACTIVATED,
    )

    with pytest.raises(AuthenticationRefusedError) as refusal:
        open_session(
            login_name="wang.li",
            password="assembly-line-3",  # pragma: allowlist secret
            users=users,
            sessions=sessions,
            policy=POLICY,
            now=MONDAY_MORNING,
        )

    assert refusal.value.code == RefusalCode.ACCOUNT_DEACTIVATED
    assert sessions.by_fingerprint == {}


def test_each_login_opens_its_own_session() -> None:
    # Two browsers, or a second shift on the same account: revoking one must not be able to
    # reach the other, so they are separate rows with separate tokens.
    users = FakeUsers()
    sessions = FakeSessions()
    users.register(login_name="wang.li", password="assembly-line-3")  # pragma: allowlist secret

    first = open_session(
        login_name="wang.li",
        password="assembly-line-3",  # pragma: allowlist secret
        users=users,
        sessions=sessions,
        policy=POLICY,
        now=MONDAY_MORNING,
    )
    second = open_session(
        login_name="wang.li",
        password="assembly-line-3",  # pragma: allowlist secret
        users=users,
        sessions=sessions,
        policy=POLICY,
        now=MONDAY_MORNING,
    )

    assert first.token != second.token
    assert first.session.id != second.session.id
    assert len(sessions.by_fingerprint) == 2
