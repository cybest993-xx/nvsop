from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Barrier

import pytest
from sqlalchemy import Engine, delete, text, update
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.device.adapters.repository import PostgresStationRepository
from factory_sop.device.model import DeviceStatus, Station
from factory_sop.identifiers import new_id
from factory_sop.template.adapters.repository import PostgresTemplateRepository
from factory_sop.template.adapters.tables import TemplateVersionRow
from factory_sop.template.artifacts import build_template_artifacts
from factory_sop.template.errors import TemplateRefusalCode, TemplateRefusedError
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
    TemplateVersion,
    TemplateVersionWriteResult,
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
    locked = repository.draft_for_publish(draft.id)
    assert reloaded is not None
    assert locked == reloaded
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


def test_a_published_template_version_round_trips_and_cannot_be_changed(
    session: DatabaseSession,
) -> None:
    repository = PostgresTemplateRepository(session)
    station = a_station()
    PostgresStationRepository(session).add(station)
    template = a_template(station)
    record = TemplateImport(
        id=new_id(),
        filename="装配一号.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        original_document=b"synthetic workbook",
        sha256="c" * 64,
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
        runtime_defaults=TemplateRuntimeDefaults(
            idle_timeout_seconds=30.0,
            step_deadline_seconds=90.0,
            disposition_policy="record",
        ),
        revision=1,
        created_by=template.created_by,
        updated_by=template.updated_by,
        created_at=NOW,
        updated_at=NOW,
        boundary=TemplateBoundaryDraft(
            start_signal=TemplateSignal(TemplateSignalKind.ACTION, 1),
            end_signals=(),
        ),
    )
    repository.add_import(record)
    repository.add_template(template)
    repository.add_draft(draft)
    session.flush()

    assert draft.boundary is not None
    built = build_template_artifacts(
        steps=draft.steps,
        ordering=draft.ordering,
        boundary=draft.boundary,
        runtime_defaults=draft.runtime_defaults,
    )
    version = TemplateVersion(
        id=new_id(),
        template_id=template.id,
        source_import_id=record.id,
        source_draft_id=draft.id,
        source_draft_revision=draft.revision,
        steps=draft.steps,
        ordering=draft.ordering,
        boundary=draft.boundary,
        runtime_defaults=draft.runtime_defaults,
        artifacts=built.artifacts,
        sha256=built.sha256,
        published_by=template.created_by,
        published_at=NOW,
    )

    first = repository.add_version(version)
    session.flush()
    session.expire_all()
    reloaded = repository.version_by_id(version.id)
    replay = repository.add_version(version)

    assert first.version == version
    assert first.created is True
    assert reloaded == version
    assert replay.version == version
    assert replay.created is False

    with pytest.raises(DatabaseError), session.begin_nested():
        session.execute(
            update(TemplateVersionRow)
            .where(TemplateVersionRow.id == version.id)
            .values(sha256="d" * 64)
        )
    assert repository.version_by_id(version.id) == version

    with pytest.raises(DatabaseError), session.begin_nested():
        session.execute(delete(TemplateVersionRow).where(TemplateVersionRow.id == version.id))
    assert repository.version_by_id(version.id) == version

    with pytest.raises(DatabaseError), session.begin_nested():
        session.execute(text("TRUNCATE template_version"))
    assert repository.version_by_id(version.id) == version


def test_concurrent_version_inserts_return_one_postgres_winner(engine: Engine) -> None:
    station = a_station()
    template = a_template(station)
    record = TemplateImport(
        id=new_id(),
        filename="并发发布.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        original_document=b"synthetic workbook",
        sha256="e" * 64,
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
        runtime_defaults=TemplateRuntimeDefaults(
            idle_timeout_seconds=30.0,
            step_deadline_seconds=90.0,
            disposition_policy="record",
        ),
        revision=2,
        created_by=template.created_by,
        updated_by=template.updated_by,
        created_at=NOW,
        updated_at=NOW,
        boundary=TemplateBoundaryDraft(
            start_signal=TemplateSignal(TemplateSignalKind.ACTION, 1),
            end_signals=(),
        ),
    )
    assert draft.boundary is not None
    built = build_template_artifacts(
        steps=draft.steps,
        ordering=draft.ordering,
        boundary=draft.boundary,
        runtime_defaults=draft.runtime_defaults,
    )
    version = TemplateVersion(
        id=new_id(),
        template_id=template.id,
        source_import_id=record.id,
        source_draft_id=draft.id,
        source_draft_revision=draft.revision,
        steps=draft.steps,
        ordering=draft.ordering,
        boundary=draft.boundary,
        runtime_defaults=draft.runtime_defaults,
        artifacts=built.artifacts,
        sha256=built.sha256,
        published_by=template.created_by,
        published_at=NOW,
    )
    competing = replace(version, id=new_id())

    try:
        with DatabaseSession(engine) as setup:
            PostgresStationRepository(setup).add(station)
            repository = PostgresTemplateRepository(setup)
            repository.add_import(record)
            repository.add_template(template)
            repository.add_draft(draft)
            setup.commit()

        barrier = Barrier(2)

        def add(candidate: TemplateVersion) -> TemplateVersionWriteResult:
            with DatabaseSession(engine) as database:
                barrier.wait()
                result = PostgresTemplateRepository(database).add_version(candidate)
                database.commit()
                return result

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(add, (version, competing)))

        assert results[0].version == results[1].version
        assert {result.created for result in results} == {True, False}
    finally:
        with engine.begin() as cleanup:
            cleanup.execute(
                text("ALTER TABLE template_version DISABLE TRIGGER template_version_immutable")
            )
            cleanup.execute(
                text("DELETE FROM template_version WHERE source_draft_id = :draft_id"),
                {"draft_id": draft.id},
            )
            cleanup.execute(
                text("ALTER TABLE template_version ENABLE TRIGGER template_version_immutable")
            )
            cleanup.execute(
                text("DELETE FROM template_draft WHERE id = :draft_id"),
                {"draft_id": draft.id},
            )
            cleanup.execute(
                text("DELETE FROM template_sop_template WHERE id = :template_id"),
                {"template_id": template.id},
            )
            cleanup.execute(
                text("DELETE FROM template_import WHERE id = :import_id"),
                {"import_id": record.id},
            )
            cleanup.execute(
                text("DELETE FROM device_station WHERE id = :station_id"),
                {"station_id": station.id},
            )
