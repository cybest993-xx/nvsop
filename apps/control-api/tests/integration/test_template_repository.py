from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.device.adapters.repository import PostgresStationRepository
from factory_sop.device.model import DeviceStatus, Station
from factory_sop.identifiers import new_id
from factory_sop.template.adapters.repository import PostgresTemplateRepository
from factory_sop.template.errors import TemplateRefusalCode, TemplateRefusedError
from factory_sop.template.model import (
    ImportStatus,
    OrderingMode,
    SopTemplate,
    TemplateDraft,
    TemplateImport,
    TemplateRuntimeDefaults,
    TemplateStep,
)

NOW = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)


def a_station() -> Station:
    actor = new_id()
    return Station(
        id=new_id(),
        code="A-001",
        name="装配一号工位",
        tags=(),
        status=DeviceStatus.ACTIVE,
        revision=1,
        created_by=actor,
        updated_by=actor,
        created_at=NOW,
        updated_at=NOW,
    )


def a_template(station: Station) -> SopTemplate:
    actor = new_id()
    return SopTemplate(
        id=new_id(),
        station_id=station.id,
        station_code=station.code,
        station_name=station.name,
        created_by=actor,
        updated_by=actor,
        created_at=NOW,
        updated_at=NOW,
    )


def test_template_import_and_draft_survive_flush_and_reload(session: DatabaseSession) -> None:
    repository = PostgresTemplateRepository(session)
    station = a_station()
    PostgresStationRepository(session).add(station)
    template = a_template(station)
    record = TemplateImport(
        id=new_id(),
        filename="装配一号.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        original_document=b"synthetic workbook",
        sha256="a" * 64,
        status=ImportStatus.SUCCEEDED,
        errors=(),
        imported_by=template.created_by,
        imported_at=NOW,
    )
    draft = TemplateDraft(
        id=new_id(),
        template_id=template.id,
        source_import_id=record.id,
        steps=(TemplateStep(number=1, name="取料", description="(1)取料"),),
        ordering=OrderingMode.STRICT,
        runtime_defaults=TemplateRuntimeDefaults(idle_timeout_seconds=30.0),
        revision=1,
        created_by=template.created_by,
        updated_by=template.updated_by,
        created_at=NOW,
        updated_at=NOW,
    )

    repository.add_import(record)
    repository.add_template(template)
    repository.add_draft(draft)
    session.flush()
    session.expire_all()

    assert repository.import_by_id(record.id) == record
    reloaded = repository.draft_by_id(draft.id)
    assert reloaded is not None
    assert reloaded.draft == draft
    assert reloaded.template == template


def test_template_draft_save_refuses_a_stale_revision(session: DatabaseSession) -> None:
    repository = PostgresTemplateRepository(session)
    station = a_station()
    PostgresStationRepository(session).add(station)
    template = a_template(station)
    record = TemplateImport(
        id=new_id(),
        filename="装配一号.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        original_document=b"synthetic workbook",
        sha256="b" * 64,
        status=ImportStatus.SUCCEEDED,
        errors=(),
        imported_by=template.created_by,
        imported_at=NOW,
    )
    draft = TemplateDraft(
        id=new_id(),
        template_id=template.id,
        source_import_id=record.id,
        steps=(TemplateStep(number=1, name="取料", description="(1)取料"),),
        ordering=OrderingMode.STRICT,
        runtime_defaults=TemplateRuntimeDefaults(),
        revision=1,
        created_by=template.created_by,
        updated_by=template.updated_by,
        created_at=NOW,
        updated_at=NOW,
    )
    repository.add_import(record)
    repository.add_template(template)
    repository.add_draft(draft)
    session.flush()

    with pytest.raises(TemplateRefusedError) as raised:
        repository.save_draft(draft, expected_revision=2)

    assert raised.value.code is TemplateRefusalCode.STALE_REVISION
    reloaded = repository.draft_by_id(draft.id)
    assert reloaded is not None
    assert reloaded.draft == draft
