"""Control-plane contract checks for C2.2 listings and validation failures.

The use-case tests settle authorization and mutation rules. This suite stays at the HTTP seam:
list responses use the shared page envelope, invalid page parameters use the RFC 9457 problem
shape, and every C2.2 operation that FastAPI can reject with 422 documents that same shape in
OpenAPI.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import pytest
from auth_fakes import FakeRoles, FakeSessions, FakeUsers
from fastapi.testclient import TestClient
from pydantic import SecretStr

from factory_sop.app import API_PREFIX, create_app
from factory_sop.auth.adapters import dependencies
from factory_sop.auth.adapters.cookies import SESSION_COOKIE
from factory_sop.auth.model import Role, User
from factory_sop.auth.permissions import Permission
from factory_sop.auth.repository import (
    LoginNameTakenError,
    RoleCodeTakenError,
    RoleRepository,
)
from factory_sop.identifiers import new_id
from factory_sop.problem import PROBLEM_MEDIA_TYPE
from factory_sop.settings import Settings

PASSWORD = "assembly-line-3"  # pragma: allowlist secret


@dataclass
class Backend:
    """A protected HTTP application over the repository seams, with all C2.2 grants available."""

    users: FakeUsers
    roles: FakeRoles
    sessions: FakeSessions
    client: TestClient


class PermissionCatalogueRoles(FakeRoles):
    """Keep test listing data separate while granting the actor every permission."""

    def permissions_of(self, user_id: UUID) -> frozenset[Permission]:
        return frozenset(Permission)


class RacingUsers(FakeUsers):
    """Simulate another transaction winning the natural-key insert after the pre-check."""

    def by_login_name(self, login_name: str) -> User | None:
        if login_name == "new.person":
            return None
        return super().by_login_name(login_name)

    def add(self, user: User) -> None:
        if user.login_name == "new.person":
            raise LoginNameTakenError(user.login_name)
        super().add(user)


class RacingRoles(PermissionCatalogueRoles):
    """Simulate a concurrent role-code winner at the repository seam."""

    def by_code(self, code: str) -> Role | None:
        if code == "race":
            return None
        return super().by_code(code)

    def add(self, role: Role) -> None:
        if role.code == "race":
            raise RoleCodeTakenError(role.code)
        super().add(role)


def settings() -> Settings:
    return Settings(
        log_level="warning",
        database_host="postgres.internal",
        database_port=5432,
        database_name="factory_sop",
        database_user="factory_sop",
        database_password=SecretStr("hunter2"),
        session_idle_timeout_minutes=720,
        session_absolute_lifetime_minutes=43200,
        session_cookie_transport="require_https",
        csrf_secret=SecretStr("csrf-secret"),
        redis_url=SecretStr("redis://127.0.0.1:1/0"),
    )


def backend() -> Backend:
    users = FakeUsers()
    roles = PermissionCatalogueRoles(users=users)
    sessions = FakeSessions()
    users.register(login_name="administrator", password=PASSWORD)
    return Backend(
        users=users,
        roles=roles,
        sessions=sessions,
        client=application_client(users, roles, sessions),
    )


def application_client(users: FakeUsers, roles: FakeRoles, sessions: FakeSessions) -> TestClient:
    app = create_app(settings())
    app.dependency_overrides[dependencies.users] = lambda: users
    app.dependency_overrides[dependencies.sessions] = lambda: sessions
    app.dependency_overrides[dependencies.roles] = lambda: roles
    client = TestClient(app, base_url="https://testserver")
    opened = client.post(
        f"{API_PREFIX}/auth/session",
        json={"login_name": "administrator", "password": PASSWORD},
    )
    assert opened.status_code == 201
    assert client.cookies.get(SESSION_COOKIE) is not None
    return client


def add_role(roles: RoleRepository, code: str) -> None:
    roles.add(Role(id=new_id(), code=code, name=code, permissions=frozenset()))


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            f"{API_PREFIX}/auth/users",
            ["wang.li", "zhao.min"],
        ),
        (
            f"{API_PREFIX}/auth/roles",
            ["reviewer"],
        ),
        (
            f"{API_PREFIX}/auth/permissions",
            ["auth.role.view", "auth.user.delete"],
        ),
    ],
)
def test_c2_2_listings_return_the_requested_page_and_total(path: str, expected: list[str]) -> None:
    built = backend()
    if path.endswith("/users"):
        for login_name in ("zhao.min", "chen.yu", "wang.li"):
            built.users.register(login_name=login_name, password=PASSWORD)
    elif path.endswith("/roles"):
        for code in ("reviewer", "operator", "administrator"):
            add_role(built.roles, code)

    response = built.client.get(path, params={"page": 2, "page_size": 2})

    assert response.status_code == 200
    assert response.json()["items"]
    assert _listing_names(response.json()["items"], path) == expected
    assert response.json()["page"] == 2
    assert response.json()["page_size"] == 2
    # The catalog's total is the enumeration's size: each module landing registers its
    # members, so the count follows `Permission` rather than a number frozen at C2.2.
    assert response.json()["total"] == (
        4 if path.endswith("/users") else 3 if path.endswith("/roles") else len(Permission)
    )


def _listing_names(items: list[object], path: str) -> list[str]:
    if path.endswith("/users"):
        return [item["login_name"] for item in items if isinstance(item, dict)]
    if path.endswith("/roles"):
        return [item["code"] for item in items if isinstance(item, dict)]
    return [item for item in items if isinstance(item, str)]


@pytest.mark.parametrize("path", [f"{API_PREFIX}/auth/users", f"{API_PREFIX}/auth/roles"])
@pytest.mark.parametrize(
    ("params", "field"),
    [({"page": 0}, "page"), ({"page_size": 201}, "page_size")],
)
def test_an_invalid_page_uses_the_problem_contract(
    path: str, params: dict[str, int], field: str
) -> None:
    response = backend().client.get(path, params=params)

    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    assert response.json()["error_code"] == "REQUEST_INVALID"
    assert response.json()["field_errors"][0]["field"] == field


def test_an_invalid_resource_id_uses_the_problem_contract() -> None:
    built = backend()
    response = built.client.delete(
        f"{API_PREFIX}/auth/users/not-a-uuid",
        headers={"x-csrf-token": built.client.cookies["sop_csrf"]},
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    assert response.json()["error_code"] == "REQUEST_INVALID"
    assert response.json()["field_errors"][0]["field"] == "user_id"


def test_a_raced_login_name_is_a_409_problem() -> None:
    users = RacingUsers()
    roles = PermissionCatalogueRoles(users=users)
    sessions = FakeSessions()
    users.register(login_name="administrator", password=PASSWORD)
    client = application_client(users, roles, sessions)

    response = client.post(
        f"{API_PREFIX}/auth/users",
        json={
            "login_name": "new.person",
            "display_name": "新人",
            "password": PASSWORD,
        },
        headers={"x-csrf-token": client.cookies["sop_csrf"]},
    )

    assert response.status_code == 409
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    assert response.json()["error_code"] == "LOGIN_NAME_TAKEN"


def test_a_raced_role_code_is_a_409_problem() -> None:
    users = FakeUsers()
    roles = RacingRoles(users=users)
    sessions = FakeSessions()
    users.register(login_name="administrator", password=PASSWORD)
    client = application_client(users, roles, sessions)

    response = client.post(
        f"{API_PREFIX}/auth/roles",
        json={"code": "race", "name": "竞争角色", "permissions": []},
        headers={"x-csrf-token": client.cookies["sop_csrf"]},
    )

    assert response.status_code == 409
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    assert response.json()["error_code"] == "ROLE_CODE_TAKEN"


C2_2_422_ROUTES = (
    ("get", f"{API_PREFIX}/auth/permissions"),
    ("get", f"{API_PREFIX}/auth/roles"),
    ("get", f"{API_PREFIX}/auth/users"),
    ("post", f"{API_PREFIX}/auth/roles"),
    ("put", f"{API_PREFIX}/auth/roles/{{role_id}}"),
    ("delete", f"{API_PREFIX}/auth/roles/{{role_id}}"),
    ("post", f"{API_PREFIX}/auth/users"),
    ("patch", f"{API_PREFIX}/auth/users/{{user_id}}"),
    ("put", f"{API_PREFIX}/auth/users/{{user_id}}/password"),
    ("put", f"{API_PREFIX}/auth/users/{{user_id}}/status"),
    ("put", f"{API_PREFIX}/auth/users/{{user_id}}/roles"),
    ("delete", f"{API_PREFIX}/auth/users/{{user_id}}"),
)


def test_every_c2_2_validation_failure_is_documented_as_problem_json() -> None:
    document = create_app(settings()).openapi()

    for method, path in C2_2_422_ROUTES:
        response = document["paths"][path][method]["responses"]["422"]
        assert set(response["content"]) == {PROBLEM_MEDIA_TYPE}, (method, path)
        assert (
            response["content"][PROBLEM_MEDIA_TYPE]["schema"]["$ref"]
            == "#/components/schemas/ProblemDocument"
        )
