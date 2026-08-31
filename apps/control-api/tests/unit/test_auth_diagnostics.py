"""What `auth`'s diagnostic lines say, and what they must never say.

§5.15 gives every line five mandatory fields and forbids a credential in any of them. The
use-case suites next to this one assert on behavior; this one asserts on the rendered JSON,
because "no credential reaches the log" is a property of the output rather than of the return
value, and a field added later would otherwise carry one unnoticed.

The lines are captured by configuring the real renderer against a `StringIO`, not by asserting
on a mock's call arguments: what has to be free of credentials is the text that leaves the
process, and a structlog processor added later could put one back into it.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from auth_fakes import FakeSessions, FakeUsers

from factory_sop.auth.errors import AuthenticationRefusedError
from factory_sop.auth.model import SessionPolicy, UserStatus
from factory_sop.auth.tokens import SessionToken
from factory_sop.auth.usecases.sessions import open_session, restore_session, revoke_session
from factory_sop.observability import configure_logging

POLICY = SessionPolicy(idle_timeout=timedelta(hours=12), absolute_lifetime=timedelta(days=30))
MONDAY_MORNING = datetime(2026, 9, 7, 1, 0, tzinfo=UTC)
PASSWORD = "assembly-line-3"

# The five §5.15 requires on every line, whatever the event.
MANDATORY_FIELDS = {"event", "module", "correlation_id", "level", "ts"}


@pytest.fixture
def log() -> io.StringIO:
    """The stream `auth`'s logger renders into for the duration of one test."""
    stream = io.StringIO()
    configure_logging(log_level="info", stream=stream)
    return stream


def lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def test_opening_a_session_logs_who_and_which_session_but_no_credential(
    log: io.StringIO,
) -> None:
    users = FakeUsers()
    sessions = FakeSessions()
    operator = users.register(login_name="wang.li", password=PASSWORD)

    opened = open_session(
        login_name="wang.li",
        password=PASSWORD,
        users=users,
        sessions=sessions,
        policy=POLICY,
        now=MONDAY_MORNING,
    )

    (line,) = lines(log)
    assert line.keys() >= MANDATORY_FIELDS
    assert line["event"] == "auth.session.opened"
    assert line["module"] == "auth"
    assert line["user_id"] == str(operator.id)
    assert line["session_id"] == str(opened.session.id)
    assert PASSWORD not in log.getvalue()
    assert opened.token.value not in log.getvalue()


def test_a_refused_login_logs_the_login_name_and_never_the_password(log: io.StringIO) -> None:
    # `login_name` is the key an administrator investigating a lockout searches by, so it is in
    # the line on purpose. The password is not there in any form — not the value, not its
    # length, not a hash of it.
    users = FakeUsers()
    users.register(login_name="wang.li", password=PASSWORD)

    with pytest.raises(AuthenticationRefusedError):
        open_session(
            login_name="wang.li",
            password="wrong-password",
            users=users,
            sessions=FakeSessions(),
            policy=POLICY,
            now=MONDAY_MORNING,
        )

    (line,) = lines(log)
    assert line["event"] == "auth.session.refused"
    assert line["error_code"] == "CREDENTIALS_REJECTED"
    # The whole key set, not a subset: a field added to this line later has to be added here
    # too, which is the moment to notice it carries something derived from the password. A
    # `not in` on the rendered text cannot catch that on its own — a length or a hash prefix
    # does not contain the password.
    assert line.keys() == MANDATORY_FIELDS | {"error_code", "login_name"}
    assert line["login_name"] == "wang.li"
    rendered = log.getvalue()
    assert "wrong-password" not in rendered
    assert PASSWORD not in rendered


def test_a_deactivated_account_is_refused_under_its_own_code(log: io.StringIO) -> None:
    users = FakeUsers()
    users.register(login_name="wang.li", password=PASSWORD, status=UserStatus.DEACTIVATED)

    with pytest.raises(AuthenticationRefusedError):
        open_session(
            login_name="wang.li",
            password=PASSWORD,
            users=users,
            sessions=FakeSessions(),
            policy=POLICY,
            now=MONDAY_MORNING,
        )

    (line,) = lines(log)
    assert line["error_code"] == "ACCOUNT_DEACTIVATED"
    assert PASSWORD not in log.getvalue()


def test_a_refused_session_logs_the_cause_the_client_is_not_told(log: io.StringIO) -> None:
    # The client gets one code, `SESSION_INVALID`, for every unusable session. The distinction
    # an administrator needs — expired, revoked, deactivated — lives here instead.
    users = FakeUsers()
    sessions = FakeSessions()
    operator = users.register(login_name="wang.li", password=PASSWORD)
    opened = open_session(
        login_name="wang.li",
        password=PASSWORD,
        users=users,
        sessions=sessions,
        policy=POLICY,
        now=MONDAY_MORNING,
    )
    users.deactivate(operator.id)

    with pytest.raises(AuthenticationRefusedError):
        restore_session(
            token=opened.token,
            users=users,
            sessions=sessions,
            policy=POLICY,
            now=MONDAY_MORNING,
        )

    refusal = lines(log)[-1]
    assert refusal["event"] == "auth.session.refused"
    assert refusal["error_code"] == "SESSION_INVALID"
    assert refusal["reason"] == "user_deactivated"


def test_no_session_token_is_ever_rendered_into_a_line(log: io.StringIO) -> None:
    # The token is the credential the browser holds. A line carrying it would make the log a
    # place a session can be stolen from, which is the same reason the store keeps only a
    # fingerprint.
    users = FakeUsers()
    sessions = FakeSessions()
    users.register(login_name="wang.li", password=PASSWORD)
    opened = open_session(
        login_name="wang.li",
        password=PASSWORD,
        users=users,
        sessions=sessions,
        policy=POLICY,
        now=MONDAY_MORNING,
    )
    restore_session(
        token=opened.token,
        users=users,
        sessions=sessions,
        policy=POLICY,
        now=MONDAY_MORNING,
    )
    revoke_session(token=opened.token, sessions=sessions)
    with pytest.raises(AuthenticationRefusedError):
        restore_session(
            token=SessionToken.issue(),
            users=users,
            sessions=sessions,
            policy=POLICY,
            now=MONDAY_MORNING,
        )

    rendered = log.getvalue()
    assert opened.token.value not in rendered
    for line in lines(log):
        assert line.keys() >= MANDATORY_FIELDS
