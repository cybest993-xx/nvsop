"""`auth`'s HTTP surface, with the repositories replaced at their own seam.

The in-memory stand-ins are the same ones the use-case suite passes (harness §4: replace the
adapter at the seam). What this suite is about is everything between the request and that
seam — cookie attributes, the `problem+json` shape, the CSRF check, and what an anonymous
caller is told — none of which needs a database to be decided. The flow against real
PostgreSQL is `tests/integration/test_auth_http.py`.
"""

from __future__ import annotations

import io
import json
from datetime import timedelta

import pytest
from auth_fakes import FakeSessions, FakeUsers
from fastapi.testclient import TestClient
from pydantic import SecretStr

from factory_sop.app import API_PREFIX, CORRELATION_ID_HEADER, create_app
from factory_sop.auth.adapters import dependencies
from factory_sop.auth.adapters.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from factory_sop.auth.model import UserStatus
from factory_sop.observability import configure_logging
from factory_sop.problem import PROBLEM_MEDIA_TYPE
from factory_sop.settings import CookieTransport, Settings

SESSION_PATH = f"{API_PREFIX}/auth/session"
CREDENTIALS = {"login_name": "wang.li", "password": "assembly-line-3"}


def settings(
    *,
    transport: CookieTransport = "require_https",
    idle_minutes: int = 720,
    absolute_minutes: int = 43200,
) -> Settings:
    return Settings(
        log_level="info",
        database_host="postgres.internal",
        database_port=5432,
        database_name="factory_sop",
        database_user="factory_sop",
        database_password=SecretStr("hunter2"),
        session_idle_timeout_minutes=idle_minutes,
        session_absolute_lifetime_minutes=absolute_minutes,
        session_cookie_transport=transport,
        csrf_secret=SecretStr("csrf-secret"),
    )


class Backend:
    """A running application over in-memory stores, plus a client that talks to it.

    The client speaks `https`, because the cookies the deployment sets carry `Secure` and a
    browser does not send one of those back over plain HTTP. A client on `http` would have
    every request after the login arrive anonymous, and the suite would be proving the wrong
    thing about the session.
    """

    def __init__(self, *, configured: Settings | None = None) -> None:
        self.users = FakeUsers()
        self.sessions = FakeSessions()
        self.app = create_app(configured or settings())
        self.app.dependency_overrides[dependencies.users] = lambda: self.users
        self.app.dependency_overrides[dependencies.sessions] = lambda: self.sessions
        self.client = TestClient(self.app, base_url="https://testserver")

    def with_account(self, *, status: UserStatus = UserStatus.ACTIVE) -> Backend:
        self.users.register(
            login_name=CREDENTIALS["login_name"],
            password=CREDENTIALS["password"],
            status=status,
        )
        return self

    def log_in(self) -> None:
        """Log in, leaving the client holding both cookies."""
        assert self.client.post(SESSION_PATH, json=CREDENTIALS).status_code == 201

    def csrf_header(self) -> dict[str, str]:
        """The header a logged-in page sends, read from the cookie the way the page does."""
        return {CSRF_HEADER: self.client.cookies[CSRF_COOKIE]}


@pytest.fixture
def backend() -> Backend:
    return Backend().with_account()


@pytest.fixture
def log() -> io.StringIO:
    """The stream the diagnostic renderer writes into for the duration of one test."""
    stream = io.StringIO()
    configure_logging(log_level="info", stream=stream)
    return stream


def test_a_login_reports_who_the_caller_is_and_when_the_session_ends(backend: Backend) -> None:
    response = backend.client.post(SESSION_PATH, json=CREDENTIALS)

    assert response.status_code == 201
    body = response.json()
    assert body["login_name"] == "wang.li"
    assert body["display_name"] == "Wang.Li"
    assert body["user_id"] == str(next(iter(backend.users.by_id)))
    # §5.15: UTC RFC3339 with a `Z`-equivalent offset, so the Web renders it in Asia/Shanghai
    # without guessing which zone it was in.
    assert body["expires_at"].endswith("+00:00") or body["expires_at"].endswith("Z")


def test_the_session_cookie_is_httponly_and_the_csrf_cookie_is_not(backend: Backend) -> None:
    # §六. The page must not be able to read the session — that is what stops an XSS from
    # exfiltrating it — and must be able to read the CSRF token, which is how it echoes it back.
    response = backend.client.post(SESSION_PATH, json=CREDENTIALS)

    attributes = {
        name: value
        for header in response.headers.get_list("set-cookie")
        for name, value in [(header.split("=", 1)[0], header.lower())]
    }
    assert "httponly" in attributes[SESSION_COOKIE]
    assert "httponly" not in attributes[CSRF_COOKIE]


def test_both_cookies_are_secure_and_samesite_strict_by_default(backend: Backend) -> None:
    response = backend.client.post(SESSION_PATH, json=CREDENTIALS)

    for header in response.headers.get_list("set-cookie"):
        assert "secure" in header.lower()
        assert "samesite=strict" in header.lower()


def test_a_deployment_without_tls_can_be_configured_to_omit_secure() -> None:
    # A browser silently discards a `Secure` cookie sent over plain HTTP, so a developer
    # running the backend without Nginx would see a login that succeeds and then does nothing.
    # It is an explicit two-value setting rather than a default, so the insecure one is never
    # what happens by accident.
    backend = Backend(configured=settings(transport="allow_http")).with_account()

    response = backend.client.post(SESSION_PATH, json=CREDENTIALS)

    for header in response.headers.get_list("set-cookie"):
        assert "secure" not in header.lower()


def test_the_session_cookie_does_not_outlive_the_session_itself(backend: Backend) -> None:
    response = backend.client.post(SESSION_PATH, json=CREDENTIALS)

    absolute_lifetime = int(timedelta(minutes=43200).total_seconds())
    for header in response.headers.get_list("set-cookie"):
        assert f"Max-Age={absolute_lifetime}" in header


def test_the_cookie_does_not_carry_the_password_or_a_hash_of_it(backend: Backend) -> None:
    response = backend.client.post(SESSION_PATH, json=CREDENTIALS)

    stored = backend.users.by_login_name("wang.li")
    assert stored is not None
    raw = str(response.headers.get_list("set-cookie"))
    assert CREDENTIALS["password"] not in raw
    assert stored.password_hash not in raw


def test_a_wrong_password_is_refused_as_problem_json(backend: Backend) -> None:
    response = backend.client.post(
        SESSION_PATH, json={**CREDENTIALS, "password": "assembly-line-4"}
    )

    assert response.status_code == 401
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    assert response.json()["error_code"] == "CREDENTIALS_REJECTED"
    assert SESSION_COOKIE not in response.cookies


def test_a_deactivated_account_is_told_so_rather_than_told_the_password_is_wrong() -> None:
    # It reached this only by presenting the correct password, so it reveals a status to
    # someone who already holds the credential. It is a different code from a rejection because
    # retyping the password will never help — the operator has to ask an administrator.
    backend = Backend().with_account(status=UserStatus.DEACTIVATED)

    response = backend.client.post(SESSION_PATH, json=CREDENTIALS)

    assert response.status_code == 403
    assert response.json()["error_code"] == "ACCOUNT_DEACTIVATED"


def test_a_malformed_login_names_the_field_that_is_wrong(backend: Backend) -> None:
    response = backend.client.post(SESSION_PATH, json={"login_name": "wang.li"})

    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    body = response.json()
    assert body["error_code"] == "REQUEST_INVALID"
    assert [item["field"] for item in body["field_errors"]] == ["password"]


def test_an_anonymous_caller_is_refused_with_authentication_required(backend: Backend) -> None:
    response = backend.client.get(SESSION_PATH)

    assert response.status_code == 401
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    assert response.json()["error_code"] == "AUTHENTICATION_REQUIRED"


def test_a_caller_holding_an_unusable_cookie_is_refused_the_same_way(backend: Backend) -> None:
    # Consistent with the above, in shape and status: a client's next move is identical, and
    # the difference between "no cookie" and "a cookie that is no good" is not one the login
    # page acts on.
    backend.client.cookies.set(SESSION_COOKIE, "not-a-real-token")

    response = backend.client.get(SESSION_PATH)

    assert response.status_code == 401
    assert response.json()["error_code"] == "SESSION_INVALID"


def test_a_logged_in_caller_reads_their_own_session(backend: Backend) -> None:
    backend.log_in()

    response = backend.client.get(SESSION_PATH)

    assert response.status_code == 200
    assert response.json()["login_name"] == "wang.li"


def test_logging_out_revokes_the_session_and_clears_both_cookies(backend: Backend) -> None:
    backend.log_in()

    response = backend.client.delete(SESSION_PATH, headers=backend.csrf_header())

    assert response.status_code == 204
    assert backend.sessions.by_fingerprint == {}
    assert backend.client.cookies.get(SESSION_COOKIE) is None
    assert backend.client.cookies.get(CSRF_COOKIE) is None


def test_the_session_is_unusable_after_logging_out(backend: Backend) -> None:
    backend.log_in()
    header = backend.csrf_header()

    backend.client.delete(SESSION_PATH, headers=header)

    assert backend.client.get(SESSION_PATH).status_code == 401


def test_logging_out_without_a_session_is_not_an_error(backend: Backend) -> None:
    # The browser may already have dropped the cookie. Answering 401 would leave it holding a
    # cookie it cannot use and no way to be rid of it.
    assert backend.client.delete(SESSION_PATH).status_code == 204


def test_a_modifying_request_without_the_csrf_header_is_refused(backend: Backend) -> None:
    # §六 requires the check on every modifying request. `SameSite=strict` already stops the
    # browser sending the cookie cross-site; this is the second line, and it does not depend on
    # the attribute never being relaxed by some future route.
    backend.log_in()

    response = backend.client.delete(SESSION_PATH)

    assert response.status_code == 403
    assert response.json()["error_code"] == "CSRF_TOKEN_INVALID"
    assert backend.sessions.by_fingerprint != {}


def test_a_modifying_request_with_the_wrong_csrf_token_is_refused(backend: Backend) -> None:
    backend.log_in()

    response = backend.client.delete(SESSION_PATH, headers={CSRF_HEADER: "0" * 64})

    assert response.status_code == 403
    assert response.json()["error_code"] == "CSRF_TOKEN_INVALID"


def test_a_csrf_refusal_is_correlated_like_every_other_request(
    backend: Backend, log: io.StringIO
) -> None:
    # The rejection is the one security-relevant refusal, and the correlation id is how one
    # operator action is followed from the Web through Nginx into this process (§5.15). A
    # request that is refused before the correlation middleware runs would be exactly the
    # event an investigator cannot follow, so binding happens outside the CSRF check.
    backend.log_in()
    inbound = "op-action-7"

    response = backend.client.delete(
        SESSION_PATH, headers={CORRELATION_ID_HEADER: inbound, CSRF_HEADER: "0" * 64}
    )

    assert response.status_code == 403
    assert response.headers[CORRELATION_ID_HEADER] == inbound
    events = [json.loads(line) for line in log.getvalue().splitlines() if line]
    (rejected,) = [line for line in events if line["event"] == "http.request.csrf_rejected"]
    assert rejected["correlation_id"] == inbound
    (completed,) = [
        line
        for line in events
        if line["event"] == "http.request.completed" and line["method"] == "DELETE"
    ]
    assert completed["correlation_id"] == inbound
    assert completed["status_code"] == 403


def test_a_csrf_token_from_another_session_does_not_pass() -> None:
    # What a plain random double-submit value would permit: the attacker sets both halves and
    # they agree with each other. Binding the token to the session is what refuses this.
    first = Backend().with_account()
    first.log_in()
    second = Backend().with_account()
    second.log_in()

    response = second.client.delete(SESSION_PATH, headers=first.csrf_header())

    assert response.status_code == 403
    assert response.json()["error_code"] == "CSRF_TOKEN_INVALID"


def test_opening_a_session_needs_no_csrf_token(backend: Backend) -> None:
    # The one exempt modifying route: there is no session to derive a token from yet, and the
    # credential in the body is what authorizes the call.
    assert backend.client.post(SESSION_PATH, json=CREDENTIALS).status_code == 201


def test_a_read_needs_no_csrf_token(backend: Backend) -> None:
    backend.log_in()

    assert backend.client.get(SESSION_PATH).status_code == 200


def test_an_unexpected_failure_is_reported_without_saying_what_broke() -> None:
    # The Web's fallback for an unknown error needs something in the agreed shape to fall back
    # *on*; and a message assembled from an exception is how a table name or a connection
    # string reaches a browser.
    backend = Backend().with_account()

    def explode() -> FakeUsers:
        raise RuntimeError("relation auth_user does not exist on postgres.internal")

    backend.app.dependency_overrides[dependencies.users] = explode
    client = TestClient(backend.app, raise_server_exceptions=False)

    response = client.post(SESSION_PATH, json=CREDENTIALS)

    assert response.status_code == 500
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    body = response.json()
    assert body["error_code"] == "INTERNAL_ERROR"
    assert "auth_user" not in str(body)
    assert "postgres.internal" not in str(body)
