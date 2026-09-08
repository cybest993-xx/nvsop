"""模板导入的真实 PostgreSQL HTTP 边界。"""

from __future__ import annotations

import hashlib
import re
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
from factory_sop.device.adapters.repository import PostgresStationRepository
from factory_sop.device.model import DeviceStatus, Station
from factory_sop.identifiers import new_id
from factory_sop.persistence import session_factory
from factory_sop.settings import Settings
from factory_sop.template.adapters.repository import PostgresTemplateRepository
from factory_sop.template.model import (
    ImportStatus,
    OrderingMode,
    SopTemplate,
    TemplateBoundaryDraft,
    TemplateDraft,
    TemplateImport,
    TemplateRuntimeDefaults,
    TemplateSignal,
    TemplateSignalKind,
    TemplateStep,
)

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
        {Permission.TEMPLATE_DRAFT_EDIT, Permission.TEMPLATE_DRAFT_VIEW}
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


@pytest.fixture
def publishable_draft(engine: Engine) -> Iterator[tuple[UUID, UUID, UUID, UUID]]:
    now = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
    actor = ACTOR_ID
    station = Station(
        id=new_id(),
        code="HTTP-28",
        name="HTTP 版本工位",
        tags=(),
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=actor,
        updated_by=actor,
        created_at=now,
        updated_at=now,
    )
    template = SopTemplate(
        id=new_id(),
        station_id=station.id,
        station_code=station.code,
        station_name=station.name,
        created_by=actor,
        updated_by=actor,
        created_at=now,
        updated_at=now,
    )
    document = b"synthetic published workbook"
    imported = TemplateImport(
        id=new_id(),
        filename="http-publish-28.xlsx",
        content_type=XLSX,
        original_document=document,
        sha256=hashlib.sha256(document).hexdigest(),
        status=ImportStatus.SUCCEEDED,
        errors=(),
        imported_by=actor,
        imported_at=now,
    )
    draft = TemplateDraft(
        id=new_id(),
        template_id=template.id,
        source_import_id=imported.id,
        steps=(TemplateStep(number=1, name="取料", description="(1)取料"),),
        ordering=OrderingMode.STRICT,
        runtime_defaults=TemplateRuntimeDefaults(
            idle_timeout_seconds=30.0,
            step_deadline_seconds=90.0,
            disposition_policy="record",
        ),
        revision=1,
        created_by=actor,
        updated_by=actor,
        created_at=now,
        updated_at=now,
        boundary=TemplateBoundaryDraft(
            start_signal=TemplateSignal(TemplateSignalKind.ACTION, 1),
            end_signals=(),
        ),
    )
    with session_factory(engine).begin() as database:
        PostgresStationRepository(database).add(station)
        repository = PostgresTemplateRepository(database)
        repository.add_import(imported)
        repository.add_template(template)
        repository.add_draft(draft)
    ids = draft.id, template.id, imported.id, station.id
    yield ids
    draft_id, template_id, import_id, station_id = ids
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE template_version DISABLE TRIGGER template_version_immutable")
        )
        try:
            connection.execute(
                text("DELETE FROM template_version WHERE source_draft_id = :draft_id"),
                {"draft_id": draft_id},
            )
        finally:
            connection.execute(
                text("ALTER TABLE template_version ENABLE TRIGGER template_version_immutable")
            )
        connection.execute(
            text("DELETE FROM template_draft WHERE id = :draft_id"),
            {"draft_id": draft_id},
        )
        connection.execute(
            text("DELETE FROM template_sop_template WHERE id = :template_id"),
            {"template_id": template_id},
        )
        connection.execute(
            text("DELETE FROM template_import WHERE id = :import_id"),
            {"import_id": import_id},
        )
        connection.execute(
            text("DELETE FROM device_station WHERE id = :station_id"),
            {"station_id": station_id},
        )


def test_published_version_round_trips_over_real_http_and_postgres(
    client: TestClient, publishable_draft: tuple[UUID, UUID, UUID, UUID]
) -> None:
    draft_id, template_id, import_id, station_id = publishable_draft

    edit = client.patch(
        f"{API_PREFIX}/templates/drafts/{draft_id}",
        headers={"If-Match": "1"},
        json={
            "steps": [{"number": 1, "name": "取料", "description": "(1)取料"}],
            "ordering": "strict",
            "start_signal": {"kind": "external", "semantic_label": "装配开始"},
            "end_signals": [{"kind": "action", "action_number": 1}],
            "runtime_defaults": {
                "idle_timeout_seconds": 30,
                "step_deadline_seconds": 90,
                "disposition_policy": "record",
            },
        },
    )
    assert edit.status_code == 200
    edit_document = edit.json()
    updated_at = edit_document.pop("updated_at")
    assert edit_document == {
        "id": str(draft_id),
        "template_id": str(template_id),
        "source_import_id": str(import_id),
        "station_id": str(station_id),
        "station_code": "HTTP-28",
        "station_name": "HTTP 版本工位",
        "steps": [{"number": 1, "name": "取料", "description": "(1)取料"}],
        "ordering": "strict",
        "runtime_defaults": {
            "idle_timeout_seconds": 30.0,
            "step_deadline_seconds": 90.0,
            "disposition_policy": "record",
        },
        "start_signal": {"kind": "external", "semantic_label": "装配开始"},
        "end_signals": [{"kind": "action", "action_number": 1}],
        "revision": 2,
        "created_by": str(ACTOR_ID),
        "updated_by": str(ACTOR_ID),
        "created_at": "2026-09-08T01:00:00Z",
    }
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z",
        updated_at,
    )

    response = client.post(
        f"{API_PREFIX}/templates/drafts/{draft_id}/publish",
        headers={"If-Match": "2"},
    )

    assert response.status_code == 201
    version = response.json()
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z",
        version["published_at"],
    )
    version_id = version["id"]
    replay = client.post(
        f"{API_PREFIX}/templates/drafts/{draft_id}/publish",
        headers={"If-Match": "2"},
    )
    assert replay.status_code == 200
    assert replay.json() == version

    listed = client.get(f"{API_PREFIX}/templates/versions")
    assert listed.status_code == 200
    assert listed.json() == {
        "items": [version],
        "page": 1,
        "page_size": 50,
        "total": 1,
    }

    detail = client.get(f"{API_PREFIX}/templates/versions/{version_id}")
    assert detail.status_code == 200
    assert detail.json() == version

    downloaded_artifacts: dict[str, bytes] = {}
    for artifact in version["artifacts"]:
        downloaded = client.get(
            f"{API_PREFIX}/templates/versions/{version_id}/artifacts/{artifact['name']}"
        )
        assert downloaded.status_code == 200
        downloaded_artifacts[artifact["name"]] = downloaded.content
        assert len(downloaded.content) == artifact["byte_length"]
        assert downloaded.headers["content-type"].split(";")[0] == artifact["media_type"]
        assert hashlib.sha256(downloaded.content).hexdigest() == artifact["sha256"]
    assert hashlib.sha256(downloaded_artifacts["manifest.json"]).hexdigest() == version["sha256"]
