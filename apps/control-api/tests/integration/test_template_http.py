"""模板导入的真实 PostgreSQL HTTP 边界。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from time import time
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text
from template_fixtures import TemplateFixture, add_template_version, remove_template_versions

from factory_sop.app import API_PREFIX, create_app
from factory_sop.auth.adapters import dependencies as auth_dependencies
from factory_sop.auth.model import Session, User, UserStatus
from factory_sop.auth.permissions import Permission
from factory_sop.auth.usecases.sessions import RestoredSession
from factory_sop.device.adapters.repository import (
    PostgresCameraRepository,
    PostgresInferenceBackendRepository,
    PostgresInferenceHostRepository,
    PostgresStationRepository,
)
from factory_sop.device.model import (
    Camera,
    ConnectionState,
    DeviceStatus,
    InferenceBackend,
    InferenceHost,
    Station,
)
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
from nvsop_contracts import (
    HostIdentityRequest,
    generate_host_identity_key_pair,
    sign_host_identity_request,
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
        {
            Permission.TEMPLATE_DRAFT_EDIT,
            Permission.TEMPLATE_DRAFT_VIEW,
            Permission.STATION_EDIT,
            Permission.STATION_VIEW,
        }
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


def test_binding_and_signed_configuration_report_round_trip_over_http_and_postgres(
    client: TestClient, engine: Engine, publishable_draft: tuple[UUID, UUID, UUID, UUID]
) -> None:
    draft_id, _template_id, _import_id, station_id = publishable_draft
    published = client.post(
        f"{API_PREFIX}/templates/drafts/{draft_id}/publish",
        headers={"If-Match": "1"},
    )
    assert published.status_code == 201, published.text
    version = published.json()

    now = datetime(2026, 9, 8, 2, 0, tzinfo=UTC)
    key_pair = generate_host_identity_key_pair()
    host = InferenceHost(
        id=new_id(),
        name="报告推理机",
        address="10.0.8.50",
        mediamtx_address=None,
        recording_window_seconds=604800,
        disk_watermark_percent=85,
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=ACTOR_ID,
        updated_by=ACTOR_ID,
        created_at=now,
        updated_at=now,
        identity_public_key=key_pair.public_key,
    )
    backend = InferenceBackend(
        id=new_id(),
        host_id=host.id,
        base_url="http://10.0.8.50:8000",
        template_version_id=None,
        status=DeviceStatus.ACTIVE,
        connection_state=ConnectionState.UNVERIFIED,
        connection_checked_at=None,
        connection_detail=None,
        self_reported_model_ids=(),
        self_reported_at=None,
        revision=1,
        created_by=ACTOR_ID,
        updated_by=ACTOR_ID,
        created_at=now,
        updated_at=now,
    )
    camera = Camera(
        id=new_id(),
        name="报告相机",
        address="10.0.8.51",
        main_stream_path="/Streaming/Channels/101",
        sub_stream_path="/Streaming/Channels/102",
        credentials_configured=False,
        station_id=station_id,
        host_id=host.id,
        backend_id=backend.id,
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=ACTOR_ID,
        updated_by=ACTOR_ID,
        created_at=now,
        updated_at=now,
    )
    report_path = f"{API_PREFIX}/templates/configuration-reports"

    try:
        with session_factory(engine).begin() as database:
            hosts = PostgresInferenceHostRepository(database)
            backends = PostgresInferenceBackendRepository(database)
            cameras = PostgresCameraRepository(database)
            hosts.add(host)
            backends.add(backend)
            cameras.add(camera)

        binding_response = client.post(
            f"{API_PREFIX}/templates/bindings",
            headers={"If-Match": "1"},
            json={
                "station_id": str(station_id),
                "version_id": version["id"],
                "runtime_parameter_mode": "follow_template",
                "runtime_parameters": None,
            },
        )
        assert binding_response.status_code == 200, binding_response.text
        binding = binding_response.json()["desired"]
        assert binding["version_id"] == version["id"]
        assert binding["config_revision"] == 2

        before_report = client.get(f"{API_PREFIX}/templates/stations/{station_id}/configuration")
        assert before_report.status_code == 200, before_report.text
        assert before_report.json()["status"] == "waiting"
        assert before_report.json()["backends"][0]["status"] == "not_confirmed"

        def signed_report(*, digest: str, nonce: str) -> tuple[dict[str, object], dict[str, str]]:
            body: dict[str, object] = {
                "station_id": str(station_id),
                "backend_id": str(backend.id),
                "version_id": version["id"],
                "sha256": digest,
                "config_revision": binding["config_revision"],
            }
            request = HostIdentityRequest(
                method="POST",
                path=report_path,
                host_id=str(host.id),
                timestamp=int(time()),
                nonce=nonce,
                body=body,
            )
            return body, {
                "X-Inference-Host-ID": str(host.id),
                "X-Inference-Host-Timestamp": str(request.timestamp),
                "X-Inference-Host-Nonce": nonce,
                "X-Inference-Host-Signature": sign_host_identity_request(
                    request,
                    private_key=key_pair.private_key,
                ),
            }

        body, headers = signed_report(digest=version["sha256"], nonce="report-http-1")
        accepted = client.post(report_path, headers=headers, json=body)
        assert accepted.status_code == 200, accepted.text
        accepted_view = accepted.json()
        assert accepted_view["accepted"] is True
        assert accepted_view["report"]["status"] == "confirmed"
        assert accepted_view["report"]["reported_sha256"] == version["sha256"]

        nonce_replay = client.post(report_path, headers=headers, json=body)
        assert nonce_replay.status_code == 401
        assert nonce_replay.json()["error_code"] == "INFERENCE_HOST_AUTHENTICATION_FAILED"

        replay_body, replay_headers = signed_report(digest=version["sha256"], nonce="report-http-2")
        replay = client.post(report_path, headers=replay_headers, json=replay_body)
        assert replay.status_code == 200, replay.text
        assert replay.json()["accepted"] is True
        assert replay.json()["report"]["reported_at"] == accepted_view["report"]["reported_at"]

        rejected_body, rejected_headers = signed_report(digest="0" * 64, nonce="report-http-3")
        rejected = client.post(report_path, headers=rejected_headers, json=rejected_body)
        assert rejected.status_code == 200, rejected.text
        rejected_view = rejected.json()
        assert rejected_view["accepted"] is False
        assert rejected_view["rejection_code"] == "digest_mismatch"
        assert rejected_view["report"]["status"] == "rejected"
        assert rejected_view["report"]["reported_sha256"] == version["sha256"]
        assert rejected_view["report"]["rejection_code"] == "digest_mismatch"

        after_report = client.get(f"{API_PREFIX}/templates/stations/{station_id}/configuration")
        assert after_report.status_code == 200, after_report.text
        assert after_report.json()["status"] == "rejected"
        assert after_report.json()["backends"][0]["reported_sha256"] == version["sha256"]
    finally:
        with engine.begin() as cleanup:
            cleanup.execute(
                text("DELETE FROM template_configuration_report WHERE station_id = :station_id"),
                {"station_id": station_id},
            )
            cleanup.execute(
                text("DELETE FROM template_station_binding WHERE station_id = :station_id"),
                {"station_id": station_id},
            )
            cleanup.execute(
                text("DELETE FROM device_camera WHERE id = :camera_id"),
                {"camera_id": camera.id},
            )
            cleanup.execute(
                text("DELETE FROM device_inference_backend WHERE id = :backend_id"),
                {"backend_id": backend.id},
            )
            cleanup.execute(
                text("DELETE FROM device_inference_host WHERE id = :host_id"),
                {"host_id": host.id},
            )


def test_multi_backend_template_binding_switches_all_backends_over_real_http_and_postgres(
    client: TestClient, engine: Engine, publishable_draft: tuple[UUID, UUID, UUID, UUID]
) -> None:
    draft_id, _template_id, _import_id, station_id = publishable_draft
    published = client.post(
        f"{API_PREFIX}/templates/drafts/{draft_id}/publish",
        headers={"If-Match": "1"},
    )
    assert published.status_code == 201, published.text
    first_version = published.json()

    now = datetime(2026, 9, 8, 3, 0, tzinfo=UTC)
    second_fixture: TemplateFixture | None = None
    host = InferenceHost(
        id=new_id(),
        name="多后端绑定推理机",
        address="10.0.8.60",
        mediamtx_address=None,
        recording_window_seconds=604800,
        disk_watermark_percent=85,
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=ACTOR_ID,
        updated_by=ACTOR_ID,
        created_at=now,
        updated_at=now,
    )
    backend_a = InferenceBackend(
        id=new_id(),
        host_id=host.id,
        base_url="http://10.0.8.60:8000",
        template_version_id=None,
        status=DeviceStatus.ACTIVE,
        connection_state=ConnectionState.UNVERIFIED,
        connection_checked_at=None,
        connection_detail=None,
        self_reported_model_ids=(),
        self_reported_at=None,
        revision=1,
        created_by=ACTOR_ID,
        updated_by=ACTOR_ID,
        created_at=now,
        updated_at=now,
    )
    backend_b = InferenceBackend(
        id=new_id(),
        host_id=host.id,
        base_url="http://10.0.8.60:8001",
        template_version_id=None,
        status=DeviceStatus.ACTIVE,
        connection_state=ConnectionState.UNVERIFIED,
        connection_checked_at=None,
        connection_detail=None,
        self_reported_model_ids=(),
        self_reported_at=None,
        revision=1,
        created_by=ACTOR_ID,
        updated_by=ACTOR_ID,
        created_at=now,
        updated_at=now,
    )
    camera_a = Camera(
        id=new_id(),
        name="多后端绑定相机A",
        address="10.0.8.61",
        main_stream_path="/Streaming/Channels/101",
        sub_stream_path="/Streaming/Channels/102",
        credentials_configured=False,
        station_id=station_id,
        host_id=host.id,
        backend_id=backend_a.id,
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=ACTOR_ID,
        updated_by=ACTOR_ID,
        created_at=now,
        updated_at=now,
    )
    camera_b = Camera(
        id=new_id(),
        name="多后端绑定相机B",
        address="10.0.8.62",
        main_stream_path="/Streaming/Channels/201",
        sub_stream_path="/Streaming/Channels/202",
        credentials_configured=False,
        station_id=station_id,
        host_id=host.id,
        backend_id=backend_b.id,
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=ACTOR_ID,
        updated_by=ACTOR_ID,
        created_at=now,
        updated_at=now,
    )
    try:
        with session_factory(engine).begin() as database:
            PostgresInferenceHostRepository(database).add(host)
            PostgresInferenceBackendRepository(database).add(backend_a)
            PostgresInferenceBackendRepository(database).add(backend_b)
            PostgresCameraRepository(database).add(camera_a)
            PostgresCameraRepository(database).add(camera_b)
            second_fixture = add_template_version(database, station_id=station_id, now=now)

        first_binding = client.post(
            f"{API_PREFIX}/templates/bindings",
            headers={"If-Match": "1"},
            json={
                "station_id": str(station_id),
                "version_id": first_version["id"],
                "runtime_parameter_mode": "follow_template",
                "runtime_parameters": None,
            },
        )
        assert first_binding.status_code == 200, first_binding.text
        after_first = client.get(f"{API_PREFIX}/templates/stations/{station_id}/configuration")
        assert after_first.status_code == 200, after_first.text
        first_configuration = after_first.json()
        assert first_configuration["station_revision"] == 2
        assert len(first_configuration["backends"]) == 2

        assert second_fixture is not None
        second_binding = client.post(
            f"{API_PREFIX}/templates/bindings",
            headers={"If-Match": str(first_configuration["station_revision"])},
            json={
                "station_id": str(station_id),
                "version_id": str(second_fixture.version_id),
                "runtime_parameter_mode": "follow_template",
                "runtime_parameters": None,
            },
        )
        assert second_binding.status_code == 200, second_binding.text
        desired = second_binding.json()["desired"]
        assert desired["version_id"] == str(second_fixture.version_id)
        assert desired["revision"] == 2

        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, template_version_id, revision "
                    "FROM device_inference_backend "
                    "WHERE id IN (:backend_a, :backend_b) ORDER BY id"
                ),
                {"backend_a": backend_a.id, "backend_b": backend_b.id},
            ).all()
        assert len(rows) == 2
        assert {row.template_version_id for row in rows} == {second_fixture.version_id}
        assert {row.revision for row in rows} == {3}
    finally:
        with engine.begin() as cleanup:
            cleanup.execute(
                text("DELETE FROM template_configuration_report WHERE station_id = :station_id"),
                {"station_id": station_id},
            )
            cleanup.execute(
                text("DELETE FROM template_station_binding WHERE station_id = :station_id"),
                {"station_id": station_id},
            )
            cleanup.execute(
                text("DELETE FROM device_camera WHERE id IN (:camera_a, :camera_b)"),
                {"camera_a": camera_a.id, "camera_b": camera_b.id},
            )
            cleanup.execute(
                text("DELETE FROM device_inference_backend WHERE id IN (:backend_a, :backend_b)"),
                {"backend_a": backend_a.id, "backend_b": backend_b.id},
            )
            cleanup.execute(
                text("DELETE FROM device_inference_host WHERE id = :host_id"),
                {"host_id": host.id},
            )
            if second_fixture is not None:
                remove_template_versions(cleanup, (second_fixture,))
