from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from auth_fakes import FakeRoles, FakeSessions, FakeUsers
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response as HttpResponse
from pydantic import SecretStr

from factory_sop.app import API_PREFIX, create_app
from factory_sop.auth.adapters import dependencies as auth_dependencies
from factory_sop.auth.adapters.cookies import CSRF_COOKIE, CSRF_HEADER
from factory_sop.auth.api import Permission
from factory_sop.device.model import DeviceStatus, Station
from factory_sop.problem import PROBLEM_MEDIA_TYPE
from factory_sop.settings import Settings
from factory_sop.template.adapters import dependencies as template_dependencies
from factory_sop.template.artifacts import build_template_artifacts
from factory_sop.template.errors import (
    TemplateFieldError,
    TemplateRefusalCode,
    TemplateRefusedError,
)
from factory_sop.template.model import (
    ImportStatus,
    OrderingMode,
    SopTemplate,
    TemplateArtifactName,
    TemplateBoundaryDraft,
    TemplateDraft,
    TemplateDraftDocument,
    TemplateImport,
    TemplateRuntimeDefaults,
    TemplateSignal,
    TemplateSignalKind,
    TemplateStep,
    TemplateVersion,
    TemplateVersionArtifact,
    TemplateVersionWriteResult,
)
from factory_sop.template.parser import ParsedStep, ParsedWorkbook, WorkbookValidationError

TEMPLATES = f"{API_PREFIX}/templates"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CREDENTIALS = {
    "login_name": "template.admin",
    "password": "template-key-27",  # pragma: allowlist secret
}


@dataclass
class FakeStations:
    rows: dict[str, Station] = field(default_factory=dict)

    def by_code(self, code: str) -> Station | None:
        return self.rows.get(code)


@dataclass
class FakeTemplateRepository:
    imports: dict[UUID, TemplateImport] = field(default_factory=dict)
    templates: dict[UUID, SopTemplate] = field(default_factory=dict)
    drafts: dict[UUID, TemplateDraft] = field(default_factory=dict)
    versions: dict[UUID, TemplateVersion] = field(default_factory=dict)

    def add_import(self, record: TemplateImport) -> None:
        self.imports[record.id] = record

    def add_template(self, template: SopTemplate) -> None:
        self.templates[template.id] = template

    def add_draft(self, draft: TemplateDraft) -> None:
        self.drafts[draft.id] = draft

    def import_by_id(self, import_id: UUID) -> TemplateImport | None:
        return self.imports.get(import_id)

    def draft_by_id(self, draft_id: UUID) -> TemplateDraftDocument | None:
        draft = self.drafts.get(draft_id)
        if draft is None:
            return None
        return TemplateDraftDocument(template=self.templates[draft.template_id], draft=draft)

    def draft_for_publish(self, draft_id: UUID) -> TemplateDraftDocument | None:
        return self.draft_by_id(draft_id)

    def save_draft(self, draft: TemplateDraft, *, expected_revision: int) -> None:
        stored = self.drafts.get(draft.id)
        if stored is None:
            raise TemplateRefusedError(TemplateRefusalCode.DRAFT_NOT_FOUND)
        if stored.revision != expected_revision:
            raise TemplateRefusedError(TemplateRefusalCode.STALE_REVISION)
        self.drafts[draft.id] = draft

    def page_drafts(self, *, page: int, page_size: int) -> tuple[list[TemplateDraftDocument], int]:
        documents = [self.draft_by_id(draft_id) for draft_id in self.drafts]
        present = [document for document in documents if document is not None]
        return present[(page - 1) * page_size : page * page_size], len(present)

    def page_imports(self, *, page: int, page_size: int) -> tuple[list[TemplateImport], int]:
        records = list(self.imports.values())
        return records[(page - 1) * page_size : page * page_size], len(records)

    def version_by_source(self, *, draft_id: UUID, revision: int) -> TemplateVersion | None:
        return next(
            (
                version
                for version in self.versions.values()
                if version.source_draft_id == draft_id and version.source_draft_revision == revision
            ),
            None,
        )

    def add_version(self, version: TemplateVersion) -> TemplateVersionWriteResult:
        existing = self.version_by_source(
            draft_id=version.source_draft_id,
            revision=version.source_draft_revision,
        )
        if existing is not None:
            return TemplateVersionWriteResult(version=existing, created=False)
        self.versions[version.id] = version
        return TemplateVersionWriteResult(version=version, created=True)

    def version_by_id(self, version_id: UUID) -> TemplateVersion | None:
        return self.versions.get(version_id)

    def page_versions(self, *, page: int, page_size: int) -> tuple[list[TemplateVersion], int]:
        versions = sorted(
            self.versions.values(),
            key=lambda version: (version.published_at, version.id),
            reverse=True,
        )
        return versions[(page - 1) * page_size : page * page_size], len(versions)

    def artifact_by_name(
        self, *, version_id: UUID, name: TemplateArtifactName
    ) -> TemplateVersionArtifact | None:
        version = self.versions.get(version_id)
        if version is None:
            return None
        return next((artifact for artifact in version.artifacts if artifact.name is name), None)


@dataclass
class Center:
    users: FakeUsers = field(default_factory=FakeUsers)
    sessions: FakeSessions = field(default_factory=FakeSessions)
    stations: FakeStations = field(default_factory=FakeStations)
    templates: FakeTemplateRepository = field(default_factory=FakeTemplateRepository)
    granted: frozenset[Permission] = frozenset()
    app: FastAPI = field(init=False)
    client: TestClient = field(init=False)

    def __post_init__(self) -> None:
        self.app = create_app(
            Settings(
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
        )
        self.app.dependency_overrides[auth_dependencies.users] = lambda: self.users
        self.app.dependency_overrides[auth_dependencies.sessions] = lambda: self.sessions
        self.app.dependency_overrides[auth_dependencies.roles] = lambda: FakeRoles(users=self.users)
        self.app.dependency_overrides[auth_dependencies.granted_permissions] = lambda: self.granted
        self.app.dependency_overrides[template_dependencies.stations] = lambda: self.stations
        self.app.dependency_overrides[template_dependencies.templates] = lambda: self.templates
        self.client = TestClient(self.app, base_url="https://testserver")

    def log_in(self, *permissions: Permission) -> Center:
        self.users.register(login_name=CREDENTIALS["login_name"], password=CREDENTIALS["password"])
        self.granted = frozenset(permissions)
        assert self.client.post(f"{API_PREFIX}/auth/session", json=CREDENTIALS).status_code == 201
        return self

    def send(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        **kwargs: Any,  # noqa: ANN401 — passthrough to the HTTP client
    ) -> HttpResponse:
        injected = dict(headers or {})
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            injected[CSRF_HEADER] = self.client.cookies[CSRF_COOKIE]
        return self.client.request(method, path, headers=injected, **kwargs)


def station() -> Station:
    now = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
    actor = UUID("00000000-0000-0000-0000-000000000001")
    return Station(
        id=UUID("00000000-0000-0000-0000-000000000002"),
        code="A-001",
        name="装配一号工位",
        tags=(),
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=actor,
        updated_by=actor,
        created_at=now,
        updated_at=now,
    )


def workbook() -> ParsedWorkbook:
    return ParsedWorkbook(
        station_code="A-001",
        station_name="装配一号工位",
        steps=(
            ParsedStep(number=1, name="取料", description="(1)取料"),
            ParsedStep(number=2, name="安装", description="(2)安装"),
        ),
    )


def test_import_route_returns_a_new_draft_and_keeps_the_original_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    center = Center()
    center.stations.rows["A-001"] = station()
    center.log_in(Permission.TEMPLATE_DRAFT_EDIT, Permission.TEMPLATE_DRAFT_VIEW)
    monkeypatch.setattr(
        "factory_sop.template.usecases.drafts.parse_workbook",
        lambda _document: workbook(),
    )

    response = center.send(
        "POST",
        f"{TEMPLATES}/imports",
        params={"filename": "装配一号.xlsx"},
        headers={"Content-Type": XLSX},
        content=b"synthetic workbook",
    )

    assert response.status_code == 201
    body = response.json()
    draft_id = UUID(body["draft"]["id"])
    import_id = UUID(body["import_record"]["id"])
    actor = center.users.by_login_name(CREDENTIALS["login_name"])
    assert actor is not None
    timestamp = body["import_record"]["imported_at"]
    assert body == {
        "import_record": {
            "id": str(import_id),
            "filename": "装配一号.xlsx",
            "content_type": XLSX,
            "sha256": hashlib.sha256(b"synthetic workbook").hexdigest(),
            "status": ImportStatus.SUCCEEDED.value,
            "errors": [],
            "imported_by": str(actor.id),
            "imported_at": timestamp,
        },
        "draft": {
            "id": str(draft_id),
            "template_id": body["draft"]["template_id"],
            "source_import_id": str(import_id),
            "station_id": str(center.stations.rows["A-001"].id),
            "station_code": "A-001",
            "station_name": "装配一号工位",
            "steps": [
                {"number": 1, "name": "取料", "description": "(1)取料"},
                {"number": 2, "name": "安装", "description": "(2)安装"},
            ],
            "ordering": OrderingMode.STRICT.value,
            "runtime_defaults": {
                "idle_timeout_seconds": None,
                "step_deadline_seconds": None,
                "disposition_policy": None,
            },
            "start_signal": None,
            "end_signals": None,
            "revision": 1,
            "created_by": str(actor.id),
            "updated_by": str(actor.id),
            "created_at": timestamp,
            "updated_at": timestamp,
        },
    }
    assert center.templates.drafts[draft_id].source_import_id == import_id
    assert center.templates.imports[import_id].original_document == b"synthetic workbook"
    download = center.send("GET", f"{TEMPLATES}/imports/{import_id}/document")
    assert download.status_code == 200
    assert download.content == b"synthetic workbook"
    assert download.headers["content-type"] == XLSX


def test_publish_route_returns_an_immutable_version_and_artifact_metadata() -> None:
    center = Center()
    center.log_in(Permission.TEMPLATE_DRAFT_EDIT, Permission.TEMPLATE_DRAFT_VIEW)
    actor = center.users.by_login_name(CREDENTIALS["login_name"])
    assert actor is not None
    template_id = UUID("00000000-0000-0000-0000-000000000320")
    draft_id = UUID("00000000-0000-0000-0000-000000000321")
    source_import_id = UUID("00000000-0000-0000-0000-000000000322")
    station_id = UUID("00000000-0000-0000-0000-000000000323")
    timestamp = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
    center.templates.templates[template_id] = SopTemplate(
        id=template_id,
        station_id=station_id,
        station_code="A-001",
        station_name="装配一号工位",
        created_by=actor.id,
        updated_by=actor.id,
        created_at=timestamp,
        updated_at=timestamp,
    )
    draft = TemplateDraft(
        id=draft_id,
        template_id=template_id,
        source_import_id=source_import_id,
        steps=(
            TemplateStep(number=1, name="取料", description="(1)取料"),
            TemplateStep(number=2, name="安装", description="(2)安装"),
        ),
        ordering=OrderingMode.STRICT,
        runtime_defaults=TemplateRuntimeDefaults(
            idle_timeout_seconds=30.0,
            step_deadline_seconds=90.0,
            disposition_policy="record",
        ),
        boundary=TemplateBoundaryDraft(
            start_signal=TemplateSignal(TemplateSignalKind.ACTION, 1),
            end_signals=(),
        ),
        revision=1,
        created_by=actor.id,
        updated_by=actor.id,
        created_at=timestamp,
        updated_at=timestamp,
    )
    center.templates.drafts[draft_id] = draft

    response = center.send(
        "POST",
        f"{TEMPLATES}/drafts/{draft_id}/publish",
        headers={"If-Match": "1"},
    )

    assert response.status_code == 201
    version = response.json()
    assert draft.boundary is not None
    built = build_template_artifacts(
        steps=draft.steps,
        ordering=draft.ordering,
        boundary=draft.boundary,
        runtime_defaults=draft.runtime_defaults,
    )
    expected_artifacts = [
        {
            "name": artifact.name.value,
            "media_type": artifact.media_type,
            "byte_length": artifact.byte_length,
            "sha256": artifact.sha256,
        }
        for artifact in built.artifacts
    ]
    assert version == {
        "id": version["id"],
        "template_id": str(template_id),
        "source_import_id": str(source_import_id),
        "source_draft_id": str(draft_id),
        "source_draft_revision": 1,
        "steps": [
            {"number": 1, "name": "取料", "description": "(1)取料"},
            {"number": 2, "name": "安装", "description": "(2)安装"},
        ],
        "ordering": "strict",
        "start_signal": {"kind": "action", "action_number": 1},
        "end_signals": [],
        "runtime_defaults": {
            "idle_timeout_seconds": 30.0,
            "step_deadline_seconds": 90.0,
            "disposition_policy": "record",
        },
        "artifacts": expected_artifacts,
        "sha256": built.sha256,
        "published_by": str(actor.id),
        "published_at": version["published_at"],
    }
    version_id = UUID(version["id"])
    replay = center.send(
        "POST",
        f"{TEMPLATES}/drafts/{draft_id}/publish",
        headers={"If-Match": "1"},
    )
    assert replay.status_code == 200
    assert replay.json() == version

    detail = center.send("GET", f"{TEMPLATES}/versions/{version_id}")
    assert detail.status_code == 200
    assert detail.json() == version

    artifact = center.send("GET", f"{TEMPLATES}/versions/{version_id}/artifacts/actions.json")
    assert artifact.status_code == 200
    assert artifact.headers["content-type"] == "application/json"
    assert artifact.content == built.artifacts[0].content


def test_publish_route_refuses_an_incomplete_boundary() -> None:
    center = Center()
    center.log_in(Permission.TEMPLATE_DRAFT_EDIT)
    actor = center.users.by_login_name(CREDENTIALS["login_name"])
    assert actor is not None
    template_id = UUID("00000000-0000-0000-0000-000000000330")
    draft_id = UUID("00000000-0000-0000-0000-000000000331")
    timestamp = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
    center.templates.templates[template_id] = SopTemplate(
        id=template_id,
        station_id=UUID("00000000-0000-0000-0000-000000000332"),
        station_code="A-001",
        station_name="装配一号工位",
        created_by=actor.id,
        updated_by=actor.id,
        created_at=timestamp,
        updated_at=timestamp,
    )
    center.templates.drafts[draft_id] = TemplateDraft(
        id=draft_id,
        template_id=template_id,
        source_import_id=UUID("00000000-0000-0000-0000-000000000333"),
        steps=(TemplateStep(number=1, name="取料", description="(1)取料"),),
        ordering=OrderingMode.STRICT,
        runtime_defaults=TemplateRuntimeDefaults(30.0, 90.0, "record"),
        boundary=TemplateBoundaryDraft(start_signal=None, end_signals=None),
        revision=1,
        created_by=actor.id,
        updated_by=actor.id,
        created_at=timestamp,
        updated_at=timestamp,
    )

    response = center.send(
        "POST", f"{TEMPLATES}/drafts/{draft_id}/publish", headers={"If-Match": "1"}
    )

    assert response.status_code == 422
    assert response.json() == {
        "type": "about:blank",
        "title": "模板版本发布校验失败",
        "status": 422,
        "error_code": "TEMPLATE_VERSION_INVALID",
        "field_errors": [
            {"field": "草稿.start_signal", "message": "必须声明开始信号"},
            {
                "field": "草稿.end_signals",
                "message": "必须明确声明结束信号列表，可以为空",
            },
        ],
    }
    assert center.templates.versions == {}


def test_download_route_normalizes_a_persisted_non_xlsx_content_type() -> None:
    center = Center()
    center.log_in(Permission.TEMPLATE_DRAFT_VIEW)
    actor = center.users.by_login_name(CREDENTIALS["login_name"])
    assert actor is not None
    imported_at = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
    import_id = UUID("00000000-0000-0000-0000-000000000306")
    center.templates.imports[import_id] = TemplateImport(
        id=import_id,
        filename="历史记录.xlsx",
        content_type="application/octet-stream",
        original_document=b"retained workbook",
        sha256=hashlib.sha256(b"retained workbook").hexdigest(),
        status=ImportStatus.FAILED,
        errors=(),
        imported_by=actor.id,
        imported_at=imported_at,
    )

    response = center.send("GET", f"{TEMPLATES}/imports/{import_id}/document")

    assert response.status_code == 200
    assert response.content == b"retained workbook"
    assert response.headers["content-type"] == XLSX


def test_edit_route_uses_if_match_and_refuses_the_second_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    center = Center()
    center.stations.rows["A-001"] = station()
    center.log_in(Permission.TEMPLATE_DRAFT_EDIT)
    monkeypatch.setattr(
        "factory_sop.template.usecases.drafts.parse_workbook",
        lambda _document: workbook(),
    )
    created = center.send(
        "POST",
        f"{TEMPLATES}/imports",
        params={"filename": "装配一号.xlsx"},
        headers={"Content-Type": XLSX},
        content=b"synthetic workbook",
    ).json()
    draft_id = created["draft"]["id"]
    submitted = {
        "steps": [{"number": 1, "name": "取料确认", "description": "(1)取料确认"}],
        "ordering": "unordered",
        "runtime_defaults": {
            "idle_timeout_seconds": 30,
            "step_deadline_seconds": 90,
            "disposition_policy": "record",
        },
    }

    edited = center.send(
        "PATCH",
        f"{TEMPLATES}/drafts/{draft_id}",
        headers={"If-Match": "1"},
        json=submitted,
    )
    stale = center.send(
        "PATCH",
        f"{TEMPLATES}/drafts/{draft_id}",
        headers={"If-Match": "1"},
        json=submitted,
    )

    assert edited.status_code == 200
    assert edited.json()["revision"] == 2
    assert stale.status_code == 409
    assert stale.json()["error_code"] == "STALE_REVISION"
    assert stale.headers["content-type"] == PROBLEM_MEDIA_TYPE


def test_edit_route_rejects_non_contiguous_step_numbers() -> None:
    center = Center()
    center.stations.rows["A-001"] = station()
    center.log_in(Permission.TEMPLATE_DRAFT_EDIT)
    draft = TemplateDraft(
        id=UUID("00000000-0000-0000-0000-000000000301"),
        template_id=UUID("00000000-0000-0000-0000-000000000302"),
        source_import_id=UUID("00000000-0000-0000-0000-000000000303"),
        steps=(TemplateStep(number=1, name="取料", description="(1)取料"),),
        ordering=OrderingMode.STRICT,
        runtime_defaults=TemplateRuntimeDefaults(),
        revision=1,
        created_by=UUID("00000000-0000-0000-0000-000000000304"),
        updated_by=UUID("00000000-0000-0000-0000-000000000304"),
        created_at=datetime(2026, 9, 8, 1, 0, tzinfo=UTC),
        updated_at=datetime(2026, 9, 8, 1, 0, tzinfo=UTC),
    )
    template = SopTemplate(
        id=draft.template_id,
        station_id=UUID("00000000-0000-0000-0000-000000000305"),
        station_code="A-001",
        station_name="装配一号工位",
        created_by=draft.created_by,
        updated_by=draft.updated_by,
        created_at=draft.created_at,
        updated_at=draft.updated_at,
    )
    center.templates.templates[template.id] = template
    center.templates.drafts[draft.id] = draft

    response = center.send(
        "PATCH",
        f"{TEMPLATES}/drafts/{draft.id}",
        headers={"If-Match": "1"},
        json={
            "steps": [
                {"number": 1, "name": "取料", "description": "(1)取料"},
                {"number": 3, "name": "安装", "description": "(3)安装"},
            ],
            "ordering": "strict",
            "runtime_defaults": {
                "idle_timeout_seconds": None,
                "step_deadline_seconds": None,
                "disposition_policy": None,
            },
        },
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "REQUEST_INVALID"
    assert center.templates.drafts[draft.id] == draft


def test_edit_route_reports_a_blank_disposition_policy_as_request_invalid() -> None:
    center = Center()
    center.log_in(Permission.TEMPLATE_DRAFT_EDIT)

    response = center.send(
        "PATCH",
        f"{TEMPLATES}/drafts/{UUID(int=1)}",
        headers={"If-Match": "1"},
        json={
            "steps": [{"number": 1, "name": "取料", "description": "(1)取料"}],
            "ordering": "strict",
            "runtime_defaults": {
                "idle_timeout_seconds": None,
                "step_deadline_seconds": None,
                "disposition_policy": "   ",
            },
        },
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "REQUEST_INVALID"


def test_invalid_import_is_a_problem_but_the_failed_import_record_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    center = Center()
    center.log_in(Permission.TEMPLATE_DRAFT_EDIT)
    error = TemplateFieldError("步骤表", 3, "步骤描述", "必须符合基座动作编码格式")
    monkeypatch.setattr(
        "factory_sop.template.usecases.drafts.parse_workbook",
        lambda _document: (_ for _ in ()).throw(WorkbookValidationError((error,))),
    )

    response = center.send(
        "POST",
        f"{TEMPLATES}/imports",
        params={"filename": "bad.xlsx"},
        headers={"Content-Type": XLSX},
        content=b"bad workbook",
    )

    assert response.status_code == 422
    assert response.json()["field_errors"] == [
        {"field": "步骤表[3].步骤描述", "message": "必须符合基座动作编码格式"}
    ]
    assert len(center.templates.imports) == 1
    assert next(iter(center.templates.imports.values())).status is ImportStatus.FAILED


def test_template_routes_declare_their_permission() -> None:
    center = Center()
    schema = center.app.openapi()

    declared = {
        f"{method.upper()} {path.removeprefix(API_PREFIX)}": operation["x-required-permission"]
        for path, item in schema["paths"].items()
        if "/templates" in path
        for method, operation in item.items()
        if method in {"get", "post", "patch"}
    }

    assert declared == {
        "POST /templates/imports": Permission.TEMPLATE_DRAFT_EDIT.value,
        "GET /templates/imports": Permission.TEMPLATE_DRAFT_VIEW.value,
        "GET /templates/imports/{import_id}": Permission.TEMPLATE_DRAFT_VIEW.value,
        "GET /templates/imports/{import_id}/document": Permission.TEMPLATE_DRAFT_VIEW.value,
        "GET /templates/drafts": Permission.TEMPLATE_DRAFT_VIEW.value,
        "GET /templates/drafts/{draft_id}": Permission.TEMPLATE_DRAFT_VIEW.value,
        "PATCH /templates/drafts/{draft_id}": Permission.TEMPLATE_DRAFT_EDIT.value,
        "POST /templates/drafts/{draft_id}/publish": Permission.TEMPLATE_DRAFT_EDIT.value,
        "GET /templates/versions": Permission.TEMPLATE_DRAFT_VIEW.value,
        "GET /templates/versions/{version_id}": Permission.TEMPLATE_DRAFT_VIEW.value,
        "GET /templates/versions/{version_id}/artifacts/{name}": (
            Permission.TEMPLATE_DRAFT_VIEW.value
        ),
    }


def test_original_download_has_an_explicit_binary_openapi_contract() -> None:
    center = Center()
    operation = center.app.openapi()["paths"][f"{TEMPLATES}/imports/{{import_id}}/document"]["get"]

    assert operation["responses"]["200"]["content"][XLSX]["schema"] == {
        "type": "string",
        "format": "binary",
    }


def test_template_list_requires_its_view_permission_even_for_an_editor() -> None:
    center = Center()
    center.log_in(Permission.TEMPLATE_DRAFT_EDIT)

    response = center.send("GET", f"{TEMPLATES}/drafts")

    assert response.status_code == 403
    assert response.json()["error_code"] == "PERMISSION_DENIED"


@pytest.mark.parametrize(
    "path",
    [
        f"{TEMPLATES}/versions",
        f"{TEMPLATES}/versions/{UUID(int=1)}",
        f"{TEMPLATES}/versions/{UUID(int=1)}/artifacts/actions.json",
    ],
)
def test_template_version_reads_require_view_permission_even_for_an_editor(path: str) -> None:
    center = Center()
    center.log_in(Permission.TEMPLATE_DRAFT_EDIT)

    response = center.send("GET", path)

    assert response.status_code == 403
    assert response.json()["error_code"] == "PERMISSION_DENIED"
