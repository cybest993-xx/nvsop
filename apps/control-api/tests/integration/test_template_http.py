"""模板导入的真实 PostgreSQL HTTP 边界。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text

from factory_sop.app import API_PREFIX, create_app
from factory_sop.auth.adapters import dependencies as auth_dependencies
from factory_sop.auth.model import Session, User, UserStatus
from factory_sop.auth.permissions import Permission
from factory_sop.auth.usecases.sessions import RestoredSession
from factory_sop.persistence import session_factory
from factory_sop.settings import Settings

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ACTOR_ID = UUID("00000000-0000-0000-0000-000000000027")
ACTOR = User(
    id=ACTOR_ID,
    login_name="template-http-test",
    display_name="模板 HTTP 测试",
    # 此字段不会参与本测试的认证。
    password_hash="unused",  # pragma: allowlist secret
    status=UserStatus.ACTIVE,
)
RESTORED = RestoredSession(
    session=Session(
        id=UUID("00000000-0000-0000-0000-000000000028"),
        user_id=ACTOR_ID,
        token_fingerprint="unused",
        created_at=datetime(2026, 9, 8, tzinfo=UTC),
        last_used_at=datetime(2026, 9, 8, tzinfo=UTC),
    ),
    user=ACTOR,
)


@pytest.fixture
def client(engine: Engine) -> Iterator[TestClient]:
    settings = Settings(
        log_level="warning",
        database_host="unused",
        database_port=5432,
        database_name="unused",
        database_user="unused",
        database_password=SecretStr("unused"),
        session_idle_timeout_minutes=720,
        session_absolute_lifetime_minutes=43200,
        session_cookie_transport="require_https",
        csrf_secret=SecretStr("csrf-secret"),
    )
    app = create_app(settings)
    app.state.session_factory = session_factory(engine)
    app.dependency_overrides[auth_dependencies.authenticated_caller] = lambda: RESTORED
    app.dependency_overrides[auth_dependencies.granted_permissions] = lambda: frozenset(
        {Permission.TEMPLATE_DRAFT_EDIT}
    )
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client


def test_invalid_import_returns_422_after_the_failed_record_commits(
    client: TestClient, engine: Engine
) -> None:
    response = client.post(
        f"{API_PREFIX}/templates/imports",
        params={"filename": "invalid-http.xlsx"},
        headers={"Content-Type": XLSX},
        content=b"not an xlsx workbook",
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "TEMPLATE_IMPORT_INVALID"
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT original_document, status, errors "
                "FROM template_import WHERE filename = :filename"
            ),
            {"filename": "invalid-http.xlsx"},
        ).one()
    assert bytes(row.original_document) == b"not an xlsx workbook"
    assert row.status == "failed"
    assert row.errors

    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM template_import WHERE filename = :filename"),
            {"filename": "invalid-http.xlsx"},
        )
