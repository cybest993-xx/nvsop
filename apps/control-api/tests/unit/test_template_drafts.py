from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

import pytest
from auth_fakes import caller_holding

from factory_sop.auth.api import Permission
from factory_sop.auth.authorization import AuthorizationRefusedError
from factory_sop.device.model import DeviceStatus, Station
from factory_sop.identifiers import new_id
from factory_sop.template.errors import TemplateRefusalCode, TemplateRefusedError
from factory_sop.template.model import (
    ImportStatus,
    OrderingMode,
    SopTemplate,
    TemplateDraft,
    TemplateDraftDocument,
    TemplateImport,
    TemplateRuntimeDefaults,
    TemplateStep,
)
from factory_sop.template.parser import ParsedStep, ParsedWorkbook
from factory_sop.template.usecases.drafts import edit_template_draft, import_template_draft

NOW = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)
CALLER = caller_holding(Permission.TEMPLATE_DRAFT_EDIT)


@dataclass
class FakeStations:
    rows: dict[str, Station] = field(default_factory=dict)

    def by_code(self, code: str) -> Station | None:
        return self.rows.get(code)


@dataclass
class FakeTemplates:
    imports: dict[UUID, TemplateImport] = field(default_factory=dict)
    templates: dict[UUID, SopTemplate] = field(default_factory=dict)
    drafts: dict[UUID, TemplateDraft] = field(default_factory=dict)

    def add_import(self, record: TemplateImport) -> None:
        self.imports[record.id] = record

    def add_template(self, template: SopTemplate) -> None:
        self.templates[template.id] = template

    def add_draft(self, draft: TemplateDraft) -> None:
        self.drafts[draft.id] = draft

    def import_by_id(self, import_id: UUID) -> TemplateImport | None:
        return self.imports.get(import_id)

    def save_draft(self, draft: TemplateDraft, *, expected_revision: int) -> None:
        stored = self.drafts.get(draft.id)
        if stored is None:
            raise TemplateRefusedError(TemplateRefusalCode.DRAFT_NOT_FOUND)
        if stored.revision != expected_revision:
            raise TemplateRefusedError(TemplateRefusalCode.STALE_REVISION)
        self.drafts[draft.id] = draft

    def draft_by_id(self, draft_id: UUID) -> TemplateDraftDocument | None:
        draft = self.drafts.get(draft_id)
        if draft is None:
            return None
        template = self.templates[draft.template_id]
        return TemplateDraftDocument(template=template, draft=draft)

    def page_drafts(self, *, page: int, page_size: int) -> tuple[list[TemplateDraftDocument], int]:
        documents = [self.draft_by_id(draft_id) for draft_id in self.drafts]
        present = [document for document in documents if document is not None]
        return present[(page - 1) * page_size : page * page_size], len(present)

    def page_imports(self, *, page: int, page_size: int) -> tuple[list[TemplateImport], int]:
        records = list(self.imports.values())
        return records[(page - 1) * page_size : page * page_size], len(records)


def station() -> Station:
    return Station(
        id=new_id(),
        code="A-001",
        name="装配一号工位",
        tags=(),
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=new_id(),
        updated_by=new_id(),
        created_at=NOW,
        updated_at=NOW,
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


def test_import_creates_a_traceable_draft_without_overwriting_an_existing_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    templates = FakeTemplates()
    stations = FakeStations(rows={"A-001": station()})
    monkeypatch.setattr(
        "factory_sop.template.usecases.drafts.parse_workbook",
        lambda _document: workbook(),
    )

    first = import_template_draft(
        document=b"first workbook",
        filename="装配一号.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        caller=CALLER,
        now=NOW,
        stations=stations,
        templates=templates,
    )
    second = import_template_draft(
        document=b"second workbook",
        filename="装配一号.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        caller=CALLER,
        now=NOW,
        stations=stations,
        templates=templates,
    )

    assert first.draft is not None
    assert second.draft is not None
    assert first.import_record.status is ImportStatus.SUCCEEDED
    assert second.import_record.status is ImportStatus.SUCCEEDED
    assert first.draft.draft.id != second.draft.draft.id
    assert len(templates.imports) == len(templates.drafts) == len(templates.templates) == 2
    assert first.import_record.original_document == b"first workbook"
    assert first.draft.draft.source_import_id == first.import_record.id
    assert first.draft.draft.steps == tuple(
        TemplateStep(number=step.number, name=step.name, description=step.description)
        for step in workbook().steps
    )
    assert first.draft.draft.revision == 1


def test_edit_replaces_steps_ordering_and_runtime_defaults_at_the_revision_seam() -> None:
    templates = FakeTemplates()
    template = SopTemplate(
        id=new_id(),
        station_id=new_id(),
        station_code="A-001",
        station_name="装配一号工位",
        created_by=CALLER.user.id,
        updated_by=CALLER.user.id,
        created_at=NOW,
        updated_at=NOW,
    )
    draft = TemplateDraft(
        id=new_id(),
        template_id=template.id,
        source_import_id=new_id(),
        steps=(),
        ordering=OrderingMode.STRICT,
        runtime_defaults=TemplateRuntimeDefaults(),
        revision=1,
        created_by=CALLER.user.id,
        updated_by=CALLER.user.id,
        created_at=NOW,
        updated_at=NOW,
    )
    templates.add_template(template)
    templates.add_draft(draft)

    edited = edit_template_draft(
        draft_id=draft.id,
        steps=tuple(
            TemplateStep(number=step.number, name=step.name, description=step.description)
            for step in workbook().steps
        ),
        ordering=OrderingMode.UNORDERED,
        runtime_defaults=TemplateRuntimeDefaults(
            idle_timeout_seconds=30.0,
            step_deadline_seconds=90.0,
            disposition_policy="record",
        ),
        expected_revision=1,
        caller=CALLER,
        now=NOW,
        templates=templates,
    )

    assert edited.draft.steps == tuple(
        TemplateStep(number=step.number, name=step.name, description=step.description)
        for step in workbook().steps
    )
    assert edited.draft.ordering is OrderingMode.UNORDERED
    assert edited.draft.runtime_defaults == TemplateRuntimeDefaults(
        idle_timeout_seconds=30.0,
        step_deadline_seconds=90.0,
        disposition_policy="record",
    )
    assert edited.draft.revision == 2
    assert templates.drafts[draft.id] == edited.draft


def test_edit_refuses_non_contiguous_step_numbers() -> None:
    templates = FakeTemplates()
    template = SopTemplate(
        id=new_id(),
        station_id=new_id(),
        station_code="A-001",
        station_name="装配一号工位",
        created_by=CALLER.user.id,
        updated_by=CALLER.user.id,
        created_at=NOW,
        updated_at=NOW,
    )
    draft = TemplateDraft(
        id=new_id(),
        template_id=template.id,
        source_import_id=new_id(),
        steps=(),
        ordering=OrderingMode.STRICT,
        runtime_defaults=TemplateRuntimeDefaults(),
        revision=1,
        created_by=CALLER.user.id,
        updated_by=CALLER.user.id,
        created_at=NOW,
        updated_at=NOW,
    )
    templates.add_template(template)
    templates.add_draft(draft)

    with pytest.raises(TemplateRefusedError) as raised:
        edit_template_draft(
            draft_id=draft.id,
            steps=(
                TemplateStep(number=1, name="取料", description="(1)取料"),
                TemplateStep(number=3, name="安装", description="(3)安装"),
            ),
            ordering=OrderingMode.STRICT,
            runtime_defaults=TemplateRuntimeDefaults(),
            expected_revision=1,
            caller=CALLER,
            now=NOW,
            templates=templates,
        )

    assert raised.value.code is TemplateRefusalCode.DRAFT_INVALID
    assert templates.drafts[draft.id] == draft


def test_edit_refuses_a_stale_revision_without_overwriting_the_draft() -> None:
    templates = FakeTemplates()
    template = SopTemplate(
        id=new_id(),
        station_id=new_id(),
        station_code="A-001",
        station_name="装配一号工位",
        created_by=CALLER.user.id,
        updated_by=CALLER.user.id,
        created_at=NOW,
        updated_at=NOW,
    )
    draft = TemplateDraft(
        id=new_id(),
        template_id=template.id,
        source_import_id=new_id(),
        steps=(),
        ordering=OrderingMode.STRICT,
        runtime_defaults=TemplateRuntimeDefaults(),
        revision=2,
        created_by=CALLER.user.id,
        updated_by=CALLER.user.id,
        created_at=NOW,
        updated_at=NOW,
    )
    templates.add_template(template)
    templates.add_draft(draft)

    with pytest.raises(TemplateRefusedError) as raised:
        edit_template_draft(
            draft_id=draft.id,
            steps=tuple(
                TemplateStep(number=step.number, name=step.name, description=step.description)
                for step in workbook().steps
            ),
            ordering=OrderingMode.STRICT,
            runtime_defaults=TemplateRuntimeDefaults(),
            expected_revision=1,
            caller=CALLER,
            now=NOW,
            templates=templates,
        )

    assert raised.value.code is TemplateRefusalCode.STALE_REVISION
    assert templates.drafts[draft.id] == draft


def test_import_and_edit_require_the_template_draft_edit_permission() -> None:
    templates = FakeTemplates()
    stations = FakeStations(rows={"A-001": station()})
    caller = caller_holding()

    with pytest.raises(AuthorizationRefusedError):
        import_template_draft(
            document=b"workbook",
            filename="装配一号.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            caller=caller,
            now=NOW,
            stations=stations,
            templates=templates,
        )

    assert templates.imports == {}


def test_invalid_import_keeps_the_original_and_the_precise_failure_record() -> None:
    templates = FakeTemplates()
    stations = FakeStations(rows={"A-001": station()})

    result = import_template_draft(
        document=b"not an xlsx",
        filename="bad.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        caller=CALLER,
        now=NOW,
        stations=stations,
        templates=templates,
    )

    assert result.draft is None
    assert result.import_record.status is ImportStatus.FAILED
    assert result.import_record.original_document == b"not an xlsx"
    assert templates.imports[result.import_record.id] == result.import_record
    assert [(error.sheet, error.row, error.field) for error in result.errors] == [
        ("工作簿", None, "文件")
    ]


def test_import_records_a_missing_device_station_as_a_located_field_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    templates = FakeTemplates()
    stations = FakeStations()
    monkeypatch.setattr(
        "factory_sop.template.usecases.drafts.parse_workbook",
        lambda _document: workbook(),
    )

    result = import_template_draft(
        document=b"workbook",
        filename="装配一号.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        caller=CALLER,
        now=NOW,
        stations=stations,
        templates=templates,
    )

    assert result.draft is None
    assert result.import_record.status is ImportStatus.FAILED
    assert result.errors == result.import_record.errors
    assert result.errors[0].sheet == "工位表"
    assert result.errors[0].row == 2
    assert result.errors[0].field == "工位号"
