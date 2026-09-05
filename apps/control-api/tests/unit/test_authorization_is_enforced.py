"""AC3, mechanically: every write route declares a permission, and the use case behind it checks
that same permission.

§5.15 puts enforcement in the `usecases/` layer and makes the route's declaration metadata for
OpenAPI. Two declarations of one fact can drift, so this suite is the mechanical check the section
asks for — and it checks the pair by *behaviour*, not by reading the source. For each write route
it sends a request as a caller holding every permission except the declared one; if the use case
does not check what the route advertises, the request succeeds and the test fails.

Two properties together are what make it a gate rather than a sample:

1. **Completeness.** `ROUTES` below is asserted to cover every modifying route the application
   serves. A new write route that nobody adds here fails the suite, so the check cannot be
   outgrown silently.
2. **Enforcement.** Each entry is exercised twice — once without the permission, expecting
   `PERMISSION_DENIED`, and once with it, expecting anything but that. The second half is what
   stops the first from passing for the wrong reason, such as a route that refuses everyone
   because its body never validates.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from auth_fakes import FakeRoles, FakeSessions, FakeUsers
from fastapi.testclient import TestClient
from pydantic import SecretStr

from factory_sop.app import API_PREFIX, MODIFYING_METHODS, create_app
from factory_sop.auth.adapters import dependencies
from factory_sop.auth.adapters.cookies import CSRF_COOKIE, CSRF_HEADER
from factory_sop.auth.adapters.dependencies import DECLARED_PERMISSION
from factory_sop.auth.model import Role, User, UserStatus
from factory_sop.auth.passwords import hash_password
from factory_sop.auth.permissions import Permission
from factory_sop.identifiers import new_id
from factory_sop.settings import Settings

# A password long enough to pass `MINIMUM_PASSWORD_LENGTH`, so a refusal in these tests is always
# the authorization one and never the password rule.
PASSWORD = "assembly-line-3"  # pragma: allowlist secret


def settings() -> Settings:
    return Settings(
        log_level="info",
        database_host="postgres.internal",
        database_port=5432,
        database_name="factory_sop",
        database_user="factory_sop",
        database_password=SecretStr("hunter2"),
        session_idle_timeout_minutes=720,
        session_absolute_lifetime_minutes=43200,
        session_cookie_transport="require_https",
        csrf_secret=SecretStr("csrf-secret"),
    )


@dataclass(frozen=True)
class Outcome:
    """Just enough of a response for these assertions: the status, and the `error_code` if any.

    Returned instead of the `httpx.Response` itself so this suite needs no annotation naming a
    package the repository does not declare as a dependency — `httpx` arrives transitively under
    `TestClient`, and importing it here would be depending on somebody else's dependency.
    """

    status_code: int
    error_code: str | None
    body: str


@dataclass(frozen=True)
class Target:
    """One write route, and a body that reaches its use case rather than the validator."""

    method: str
    template: str
    body: dict[str, object] | None = None


# Every modifying route the backend serves, other than the session ones — those are how a caller
# acquires an identity and deliberately require no permission (`auth/usecases/sessions.py`).
# `{user_id}` and `{role_id}` are filled in with the fixture's existing rows. The status `PUT`
# deactivates rather than reactivates: the deactivated branch is the one whose use case does the
# harder thing (revoking every session), and a route that refuses every caller would satisfy the
# enforcement half for the wrong reason.
ROUTES = [
    Target(
        "POST",
        "/auth/users",
        {"login_name": "new.person", "display_name": "新人", "password": PASSWORD},
    ),
    Target("PATCH", "/auth/users/{user_id}", {"display_name": "改名"}),
    Target("PUT", "/auth/users/{user_id}/password", {"password": PASSWORD}),
    Target("PUT", "/auth/users/{user_id}/status", {"status": "deactivated"}),
    Target("PUT", "/auth/users/{user_id}/roles", {"role_ids": []}),
    Target("DELETE", "/auth/users/{user_id}"),
    Target("POST", "/auth/roles", {"code": "fresh", "name": "新角色", "permissions": []}),
    Target("PUT", "/auth/roles/{role_id}", {"name": "改名", "permissions": []}),
    Target("DELETE", "/auth/roles/{role_id}"),
]

# The session resource: no permission, by design, and therefore not part of the check above.
EXEMPT = {
    ("POST", f"{API_PREFIX}/auth/session"),
    ("DELETE", f"{API_PREFIX}/auth/session"),
}


class Backend:
    """An application over in-memory stores, with the caller's permissions under test control."""

    def __init__(self) -> None:
        self.users = FakeUsers()
        self.roles = FakeRoles(users=self.users)
        self.sessions = FakeSessions()
        self.granted: frozenset[Permission] = frozenset()

        # The account the requests are made as. Its permissions come from `self.granted` rather
        # than from an assigned role, so a test can hold "everything except one" without building
        # a role for each combination.
        self.actor = User(
            id=new_id(),
            login_name="administrator",
            display_name="系统管理员",
            password_hash=hash_password(PASSWORD),
            status=UserStatus.ACTIVE,
        )
        self.users.add(self.actor)
        # A second account and a role for the `{user_id}` and `{role_id}` templates: the target of
        # the operation is never the caller, so a refusal is never the last-administrator guard.
        self.subject = self.users.register(login_name="wang.li", password=PASSWORD)
        self.role = Role(id=new_id(), code="viewer", name="只读", permissions=frozenset())
        self.roles.add(self.role)

        self.app = create_app(settings())
        self.app.dependency_overrides[dependencies.users] = lambda: self.users
        self.app.dependency_overrides[dependencies.sessions] = lambda: self.sessions
        self.app.dependency_overrides[dependencies.roles] = lambda: self.roles
        self.app.dependency_overrides[dependencies.granted_permissions] = lambda: self.granted
        self.client = TestClient(self.app, base_url="https://testserver")
        assert (
            self.client.post(
                f"{API_PREFIX}/auth/session",
                json={"login_name": "administrator", "password": PASSWORD},
            ).status_code
            == 201
        )

    def send(self, target: Target, *, granted: frozenset[Permission]) -> Outcome:
        self.granted = granted
        path = target.template.format(user_id=self.subject.id, role_id=self.role.id)
        response = self.client.request(
            target.method,
            f"{API_PREFIX}{path}",
            json=target.body,
            headers={CSRF_HEADER: self.client.cookies[CSRF_COOKIE]},
        )
        error_code: str | None = None
        if response.content:
            document = response.json()
            if isinstance(document, dict):
                code = document.get("error_code")
                error_code = code if isinstance(code, str) else None
        return Outcome(status_code=response.status_code, error_code=error_code, body=response.text)


@pytest.fixture
def backend() -> Backend:
    return Backend()


def declarations(backend: Backend) -> dict[tuple[str, str], str | None]:
    """Every modifying operation the application serves, and the permission it declares.

    Read out of the generated OpenAPI document rather than off `app.routes`: the declaration exists
    to appear in that document, so this asserts on what an integrator actually receives.
    """
    document = backend.app.openapi()
    found: dict[tuple[str, str], str | None] = {}
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            if method.upper() in MODIFYING_METHODS:
                found[(method.upper(), path)] = operation.get(DECLARED_PERMISSION)
    return found


def test_every_write_route_is_covered_by_this_suite(backend: Backend) -> None:
    # Without this, the enforcement test below is a sample of whatever someone remembered to list.
    # With it, a new write route fails the suite until it is listed and proved.
    declared = {(target.method, f"{API_PREFIX}{target.template}") for target in ROUTES}

    served = set(declarations(backend))
    # Non-empty as its own assertion: an enumeration that silently found nothing would make this
    # comparison pass between two empty sets, which is exactly the failure this suite must not have.
    assert served
    assert served - EXEMPT == declared


def test_every_write_route_declares_the_permission_it_needs(backend: Backend) -> None:
    # The declaration is what OpenAPI shows and what an integrator reads. A route without one is a
    # route whose permission is only discoverable by reading the use case.
    registered = {item.value for item in Permission}
    for (method, path), declared in declarations(backend).items():
        if (method, path) in EXEMPT:
            continue
        assert declared is not None, f"{method} {path} declares no permission"
        assert declared in registered, (
            f"{method} {path} declares {declared}, which is not registered"
        )


def declared_permission_of(backend: Backend, target: Target) -> Permission:
    """The permission the route advertises for this method and path."""
    declared = declarations(backend).get((target.method, f"{API_PREFIX}{target.template}"))
    assert declared is not None, f"no declaration for {target.method} {target.template}"
    return Permission(declared)


@pytest.mark.parametrize("target", ROUTES, ids=lambda target: f"{target.method} {target.template}")
def test_the_use_case_refuses_a_caller_without_the_declared_permission(
    backend: Backend, target: Target
) -> None:
    declared = declared_permission_of(backend, target)
    # Everything *except* the declared one. A caller with no permissions at all would be refused by
    # a use case checking some other permission too, and the test would pass without proving that
    # the route's declaration is the one being enforced.
    others = frozenset(Permission) - {declared}

    response = backend.send(target, granted=others)

    assert response.status_code == 403, response.body
    assert response.error_code == "PERMISSION_DENIED"


@pytest.mark.parametrize("target", ROUTES, ids=lambda target: f"{target.method} {target.template}")
def test_the_declared_permission_is_sufficient(backend: Backend, target: Target) -> None:
    # The other half: holding exactly what the route declares gets past authorization. Without
    # this, a route that refused every caller for an unrelated reason would satisfy the test above.
    declared = declared_permission_of(backend, target)

    response = backend.send(target, granted=frozenset({declared}))

    assert response.status_code != 403, response.body
