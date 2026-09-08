from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import UUID

import pytest
from auth_fakes import caller_holding

from factory_sop.auth.api import Permission
from factory_sop.auth.authorization import AuthorizationRefusedError
from factory_sop.identifiers import new_id
from factory_sop.observability import configure_logging
from factory_sop.template.artifacts import build_template_artifacts
from factory_sop.template.errors import TemplateRefusalCode, TemplateRefusedError
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
from factory_sop.template.usecases.versions import (
    download_template_version_artifact,
    list_template_versions,
    publish_template_version,
    read_template_version,
)

NOW = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
EDITOR = caller_holding(Permission.TEMPLATE_DRAFT_EDIT)
VIEWER = caller_holding(Permission.TEMPLATE_DRAFT_VIEW)


@dataclass
class FakeVersions:
    imports: dict[UUID, TemplateImport] = field(default_factory=dict)
    templates: dict[UUID, SopTemplate] = field(default_factory=dict)
    drafts: dict[UUID, TemplateDraft] = field(default_factory=dict)
    versions: dict[UUID, TemplateVersion] = field(default_factory=dict)
    by_source: dict[tuple[UUID, int], UUID] = field(default_factory=dict)
    missing_artifacts: set[tuple[UUID, TemplateArtifactName]] = field(default_factory=set)
    return_replay_from_add: bool = False

    def draft_by_id(self, draft_id: UUID) -> TemplateDraftDocument | None:
        draft = self.drafts.get(draft_id)
        if draft is None:
            return None
        return TemplateDraftDocument(template=self.templates[draft.template_id], draft=draft)

    def draft_for_publish(self, draft_id: UUID) -> TemplateDraftDocument | None:
        return self.draft_by_id(draft_id)

    def version_by_source(self, *, draft_id: UUID, revision: int) -> TemplateVersion | None:
        version_id = self.by_source.get((draft_id, revision))
        return self.versions.get(version_id) if version_id is not None else None

    def add_version(self, version: TemplateVersion) -> TemplateVersionWriteResult:
        existing = self.version_by_source(
            draft_id=version.source_draft_id, revision=version.source_draft_revision
        )
        if existing is not None:
            return TemplateVersionWriteResult(version=existing, created=False)
        self.versions[version.id] = version
        self.by_source[(version.source_draft_id, version.source_draft_revision)] = version.id
        return TemplateVersionWriteResult(version=version, created=not self.return_replay_from_add)

    def page_versions(self, *, page: int, page_size: int) -> tuple[list[TemplateVersion], int]:
        versions = sorted(
            self.versions.values(),
            key=lambda version: (version.published_at, version.id),
            reverse=True,
        )
        start = (page - 1) * page_size
        return versions[start : start + page_size], len(versions)

    def version_by_id(self, version_id: UUID) -> TemplateVersion | None:
        return self.versions.get(version_id)

    def artifact_by_name(
        self, *, version_id: UUID, name: TemplateArtifactName
    ) -> TemplateVersionArtifact | None:
        if (version_id, name) in self.missing_artifacts:
            return None
        version = self.versions.get(version_id)
        if version is None:
            return None
        return next((artifact for artifact in version.artifacts if artifact.name is name), None)


def complete_draft(*, revision: int = 1) -> tuple[FakeVersions, TemplateDraft]:
    repository = FakeVersions()
    actor = EDITOR.user.id
    template = SopTemplate(
        id=new_id(),
        station_id=new_id(),
        station_code="A-001",
        station_name="装配一号工位",
        created_by=actor,
        updated_by=actor,
        created_at=NOW,
        updated_at=NOW,
    )
    imported = TemplateImport(
        id=new_id(),
        filename="装配一号.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        original_document=b"synthetic workbook",
        sha256="a" * 64,
        status=ImportStatus.SUCCEEDED,
        errors=(),
        imported_by=actor,
        imported_at=NOW,
    )
    draft = TemplateDraft(
        id=new_id(),
        template_id=template.id,
        source_import_id=imported.id,
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
        revision=revision,
        created_by=actor,
        updated_by=actor,
        created_at=NOW,
        updated_at=NOW,
        boundary=TemplateBoundaryDraft(
            start_signal=TemplateSignal(TemplateSignalKind.ACTION, 1),
            end_signals=(),
        ),
    )
    repository.templates[template.id] = template
    repository.imports[imported.id] = imported
    repository.drafts[draft.id] = draft
    return repository, draft


def test_publish_generates_deterministic_frozen_artifacts() -> None:
    repository, draft = complete_draft()
    assert draft.boundary is not None
    built = build_template_artifacts(
        steps=draft.steps,
        ordering=draft.ordering,
        boundary=draft.boundary,
        runtime_defaults=draft.runtime_defaults,
    )
    assert built == build_template_artifacts(
        steps=draft.steps,
        ordering=draft.ordering,
        boundary=draft.boundary,
        runtime_defaults=draft.runtime_defaults,
    )

    result = publish_template_version(
        draft_id=draft.id,
        expected_revision=draft.revision,
        caller=EDITOR,
        now=NOW,
        templates=repository,
    )

    expected_version = TemplateVersion(
        id=result.version.id,
        template_id=draft.template_id,
        source_import_id=draft.source_import_id,
        source_draft_id=draft.id,
        source_draft_revision=draft.revision,
        steps=draft.steps,
        ordering=draft.ordering,
        boundary=draft.boundary,
        runtime_defaults=draft.runtime_defaults,
        artifacts=built.artifacts,
        sha256=built.sha256,
        published_by=EDITOR.user.id,
        published_at=NOW,
    )
    assert result == TemplateVersionWriteResult(version=expected_version, created=True)
    assert all(
        hashlib.sha256(artifact.content).hexdigest() == artifact.sha256
        for artifact in result.version.artifacts
    )


def test_an_artifact_rejects_a_digest_that_does_not_match_its_bytes() -> None:
    with pytest.raises(ValueError, match="制品摘要与内容不一致"):
        TemplateVersionArtifact(
            name=TemplateArtifactName.ACTIONS,
            media_type="application/json",
            content=b"content",
            sha256="0" * 64,
        )


def test_publish_records_a_stable_success_diagnostic_event() -> None:
    repository, draft = complete_draft()
    stream = io.StringIO()
    configure_logging(log_level="info", stream=stream)

    result = publish_template_version(
        draft_id=draft.id,
        expected_revision=draft.revision,
        caller=EDITOR,
        now=NOW,
        templates=repository,
    )

    event = json.loads(stream.getvalue().splitlines()[-1])
    event.pop("ts")
    event.pop("correlation_id")
    assert event == {
        "event": "template.version.publish.succeeded",
        "module": "template",
        "level": "info",
        "actor_id": str(EDITOR.user.id),
        "draft_id": str(draft.id),
        "version_id": str(result.version.id),
        "source_revision": "1",
        "sha256": result.version.sha256,
    }


def test_publish_rejects_an_incomplete_boundary_without_creating_a_version() -> None:
    repository, draft = complete_draft()
    stream = io.StringIO()
    configure_logging(log_level="info", stream=stream)
    repository.drafts[draft.id] = replace(
        draft, boundary=TemplateBoundaryDraft(start_signal=None, end_signals=None)
    )

    with pytest.raises(TemplateRefusedError) as raised:
        publish_template_version(
            draft_id=draft.id,
            expected_revision=draft.revision,
            caller=EDITOR,
            now=NOW,
            templates=repository,
        )

    assert raised.value.code is TemplateRefusalCode.VERSION_INVALID
    assert {(item.field, item.message) for item in raised.value.field_errors} == {
        ("start_signal", "必须声明开始信号"),
        ("end_signals", "必须明确声明结束信号列表，可以为空"),
    }
    event = json.loads(stream.getvalue().splitlines()[-1])
    event.pop("ts")
    event.pop("correlation_id")
    assert event == {
        "event": "template.version.publish.rejected",
        "module": "template",
        "level": "info",
        "error_code": TemplateRefusalCode.VERSION_INVALID.value,
        "draft_id": str(draft.id),
        "source_revision": "1",
        "actor_id": str(EDITOR.user.id),
    }
    assert repository.versions == {}


def test_publish_rejects_empty_or_non_contiguous_steps() -> None:
    for steps in (
        (),
        (
            TemplateStep(number=1, name="取料", description="(1)取料"),
            TemplateStep(number=3, name="安装", description="(3)安装"),
        ),
    ):
        repository, draft = complete_draft()
        repository.drafts[draft.id] = replace(draft, steps=steps)

        with pytest.raises(TemplateRefusedError) as raised:
            publish_template_version(
                draft_id=draft.id,
                expected_revision=draft.revision,
                caller=EDITOR,
                now=NOW,
                templates=repository,
            )

        assert raised.value.code is TemplateRefusalCode.VERSION_INVALID
        assert any(item.field == "steps" for item in raised.value.field_errors)
        assert repository.versions == {}


def test_publish_rejects_invalid_boundary_references_and_duplicates() -> None:
    repository, draft = complete_draft()
    repository.drafts[draft.id] = replace(
        draft,
        boundary=TemplateBoundaryDraft(
            start_signal=TemplateSignal(TemplateSignalKind.ACTION, 99),
            end_signals=(
                TemplateSignal(TemplateSignalKind.ACTION, 1),
                TemplateSignal(TemplateSignalKind.ACTION, 1),
            ),
        ),
    )

    with pytest.raises(TemplateRefusedError) as raised:
        publish_template_version(
            draft_id=draft.id,
            expected_revision=draft.revision,
            caller=EDITOR,
            now=NOW,
            templates=repository,
        )

    assert raised.value.code is TemplateRefusalCode.VERSION_INVALID
    assert {item.field for item in raised.value.field_errors} == {
        "start_signal",
        "end_signals[1]",
    }
    assert repository.versions == {}


def test_publish_rejects_missing_runtime_defaults() -> None:
    repository, draft = complete_draft()
    repository.drafts[draft.id] = replace(draft, runtime_defaults=TemplateRuntimeDefaults())

    with pytest.raises(TemplateRefusedError) as raised:
        publish_template_version(
            draft_id=draft.id,
            expected_revision=draft.revision,
            caller=EDITOR,
            now=NOW,
            templates=repository,
        )

    assert raised.value.code is TemplateRefusalCode.VERSION_INVALID
    assert {item.field for item in raised.value.field_errors} == {
        "idle_timeout_seconds",
        "step_deadline_seconds",
        "disposition_policy",
    }
    assert repository.versions == {}


def test_repeating_a_revision_returns_the_original_version_after_the_draft_changes() -> None:
    repository, draft = complete_draft()
    stream = io.StringIO()
    configure_logging(log_level="info", stream=stream)
    first = publish_template_version(
        draft_id=draft.id,
        expected_revision=draft.revision,
        caller=EDITOR,
        now=NOW,
        templates=repository,
    )
    repository.drafts[draft.id] = TemplateDraft(
        id=draft.id,
        template_id=draft.template_id,
        source_import_id=draft.source_import_id,
        steps=draft.steps,
        ordering=OrderingMode.UNORDERED,
        runtime_defaults=draft.runtime_defaults,
        revision=2,
        created_by=draft.created_by,
        updated_by=draft.updated_by,
        created_at=draft.created_at,
        updated_at=NOW,
        boundary=draft.boundary,
    )

    replay = publish_template_version(
        draft_id=draft.id,
        expected_revision=1,
        caller=EDITOR,
        now=datetime(2026, 9, 8, 2, 0, tzinfo=UTC),
        templates=repository,
    )

    assert replay.version == first.version
    assert replay.created is False
    events = [
        {
            key: value
            for key, value in json.loads(line).items()
            if key not in {"ts", "correlation_id"}
        }
        for line in stream.getvalue().splitlines()
    ]
    assert events == [
        {
            "event": "template.version.publish.succeeded",
            "module": "template",
            "level": "info",
            "version_id": str(first.version.id),
            "draft_id": str(draft.id),
            "source_revision": "1",
            "actor_id": str(EDITOR.user.id),
            "sha256": first.version.sha256,
        },
        {
            "event": "template.version.publish.replayed",
            "module": "template",
            "level": "info",
            "version_id": str(first.version.id),
            "draft_id": str(draft.id),
            "source_revision": "1",
            "actor_id": str(EDITOR.user.id),
            "sha256": first.version.sha256,
        },
    ]
    assert len(repository.versions) == 1


def test_a_late_idempotent_publish_records_a_replay_event() -> None:
    repository, draft = complete_draft()
    repository.return_replay_from_add = True
    stream = io.StringIO()
    configure_logging(log_level="info", stream=stream)

    result = publish_template_version(
        draft_id=draft.id,
        expected_revision=draft.revision,
        caller=EDITOR,
        now=NOW,
        templates=repository,
    )

    assert result.created is False
    event = json.loads(stream.getvalue().splitlines()[-1])
    event.pop("ts")
    event.pop("correlation_id")
    assert event == {
        "event": "template.version.publish.replayed",
        "module": "template",
        "level": "info",
        "version_id": str(result.version.id),
        "draft_id": str(draft.id),
        "source_revision": "1",
        "actor_id": str(EDITOR.user.id),
        "sha256": result.version.sha256,
    }


def test_version_history_and_artifact_download_require_view_permission() -> None:
    repository, draft = complete_draft()
    stream = io.StringIO()
    configure_logging(log_level="info", stream=stream)
    published = publish_template_version(
        draft_id=draft.id,
        expected_revision=draft.revision,
        caller=EDITOR,
        now=NOW,
        templates=repository,
    ).version

    listed, total = list_template_versions(
        caller=VIEWER, templates=repository, page=1, page_size=50
    )
    assert (listed, total) == ([published], 1)
    assert (
        read_template_version(version_id=published.id, caller=VIEWER, templates=repository)
        == published
    )
    for artifact in published.artifacts:
        downloaded = download_template_version_artifact(
            version_id=published.id,
            name=artifact.name,
            caller=VIEWER,
            templates=repository,
        )
        assert downloaded == artifact
        assert hashlib.sha256(downloaded.content).hexdigest() == downloaded.sha256

    events = [
        {
            key: value
            for key, value in json.loads(line).items()
            if key not in {"ts", "correlation_id"}
        }
        for line in stream.getvalue().splitlines()
    ]
    assert events == [
        {
            "event": "template.version.publish.succeeded",
            "module": "template",
            "level": "info",
            "version_id": str(published.id),
            "draft_id": str(draft.id),
            "source_revision": "1",
            "actor_id": str(EDITOR.user.id),
            "sha256": published.sha256,
        },
        {
            "event": "template.version.list.succeeded",
            "module": "template",
            "level": "info",
            "page": 1,
            "page_size": 50,
            "total": 1,
            "actor_id": str(VIEWER.user.id),
        },
        {
            "event": "template.version.read.succeeded",
            "module": "template",
            "level": "info",
            "version_id": str(published.id),
            "sha256": published.sha256,
            "actor_id": str(VIEWER.user.id),
        },
        *[
            {
                "event": "template.version.artifact.download.succeeded",
                "module": "template",
                "level": "info",
                "version_id": str(published.id),
                "artifact": artifact.name.value,
                "byte_length": artifact.byte_length,
                "sha256": artifact.sha256,
                "actor_id": str(VIEWER.user.id),
            }
            for artifact in published.artifacts
        ],
    ]

    for operation in (
        lambda: list_template_versions(caller=EDITOR, templates=repository, page=1, page_size=50),
        lambda: read_template_version(version_id=published.id, caller=EDITOR, templates=repository),
        lambda: download_template_version_artifact(
            version_id=published.id,
            name=TemplateArtifactName.ACTIONS,
            caller=EDITOR,
            templates=repository,
        ),
    ):
        with pytest.raises(AuthorizationRefusedError):
            operation()


def test_reading_an_unknown_version_or_artifact_returns_a_stable_refusal() -> None:
    repository, draft = complete_draft()
    published = publish_template_version(
        draft_id=draft.id,
        expected_revision=draft.revision,
        caller=EDITOR,
        now=NOW,
        templates=repository,
    ).version

    with pytest.raises(TemplateRefusedError) as missing_version:
        read_template_version(version_id=new_id(), caller=VIEWER, templates=repository)
    assert missing_version.value.code is TemplateRefusalCode.VERSION_NOT_FOUND

    repository.missing_artifacts.add((published.id, TemplateArtifactName.ACTIONS))
    with pytest.raises(TemplateRefusedError) as missing_artifact:
        download_template_version_artifact(
            version_id=published.id,
            name=TemplateArtifactName.ACTIONS,
            caller=VIEWER,
            templates=repository,
        )
    assert missing_artifact.value.code is TemplateRefusalCode.VERSION_ARTIFACT_NOT_FOUND


def test_publish_requires_edit_permission_even_when_a_version_is_known() -> None:
    repository, draft = complete_draft()

    with pytest.raises(AuthorizationRefusedError):
        publish_template_version(
            draft_id=draft.id,
            expected_revision=draft.revision,
            caller=VIEWER,
            now=NOW,
            templates=repository,
        )


def test_publish_rejects_a_stale_revision_before_validating_new_content() -> None:
    repository, draft = complete_draft()
    repository.drafts[draft.id] = TemplateDraft(
        id=draft.id,
        template_id=draft.template_id,
        source_import_id=draft.source_import_id,
        steps=draft.steps,
        ordering=draft.ordering,
        runtime_defaults=draft.runtime_defaults,
        revision=2,
        created_by=draft.created_by,
        updated_by=draft.updated_by,
        created_at=draft.created_at,
        updated_at=NOW,
        boundary=draft.boundary,
    )

    with pytest.raises(TemplateRefusedError) as raised:
        publish_template_version(
            draft_id=draft.id,
            expected_revision=1,
            caller=EDITOR,
            now=NOW,
            templates=repository,
        )

    assert raised.value.code is TemplateRefusalCode.STALE_REVISION
    assert repository.versions == {}
