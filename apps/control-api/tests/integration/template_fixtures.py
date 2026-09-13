"""Synthetic immutable template versions for device PostgreSQL integration tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from factory_sop.device.adapters.tables import StationRow
from factory_sop.device.model import DeviceStatus, RuntimeParameterMode, Station
from factory_sop.identifiers import new_id
from factory_sop.template.adapters.tables import (
    SopTemplateRow,
    TemplateDraftRow,
    TemplateImportRow,
    TemplateVersionRow,
)
from factory_sop.template.artifacts import build_template_artifacts
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
)


@dataclass(frozen=True, slots=True)
class TemplateFixture:
    """The rows needed to remove one synthetic version from a committed test database."""

    version_id: UUID
    template_id: UUID
    import_id: UUID
    draft_id: UUID
    station_id: UUID | None = None


def add_template_version(
    session: Session,
    *,
    station_id: UUID | None = None,
    now: datetime,
    version_id: UUID | None = None,
) -> TemplateFixture:
    """Persist one valid published version whose identity can be used by a device row."""
    owned_station_id: UUID | None = None
    if station_id is None:
        owned_station_id = new_id()
        station_id = owned_station_id
        station = Station(
            id=station_id,
            code=f"fixture-station-{station_id.hex[:12]}",
            name="集成测试工位",
            tags=(),
            status=DeviceStatus.ACTIVE,
            revision=1,
            created_by=new_id(),
            updated_by=new_id(),
            created_at=now,
            updated_at=now,
            runtime_parameter_mode=RuntimeParameterMode.FOLLOW_TEMPLATE,
            runtime_parameter_overrides=None,
            runtime_parameters_revision=1,
        )
        session.add(StationRow.from_domain(station))
        session.flush()

    actor = new_id()
    template_id = new_id()
    import_id = new_id()
    draft_id = new_id()
    version_id = version_id or new_id()
    steps = (TemplateStep(number=1, name="测试动作", description="(1)测试动作"),)
    boundary = TemplateBoundaryDraft(
        start_signal=TemplateSignal(TemplateSignalKind.ACTION, 1),
        end_signals=(),
    )
    runtime_defaults = TemplateRuntimeDefaults(
        idle_timeout_seconds=30.0,
        step_deadline_seconds=90.0,
        disposition_policy="record",
    )
    template = SopTemplate(
        id=template_id,
        station_id=station_id,
        station_code=f"fixture-{template_id.hex[:12]}",
        station_name="集成测试模板",
        created_by=actor,
        updated_by=actor,
        created_at=now,
        updated_at=now,
    )
    imported = TemplateImport(
        id=import_id,
        filename="integration-fixture.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        original_document=b"synthetic integration fixture",
        sha256="a" * 64,
        status=ImportStatus.SUCCEEDED,
        errors=(),
        imported_by=actor,
        imported_at=now,
    )
    draft = TemplateDraft(
        id=draft_id,
        template_id=template_id,
        source_import_id=import_id,
        steps=steps,
        ordering=OrderingMode.STRICT,
        runtime_defaults=runtime_defaults,
        revision=1,
        created_by=actor,
        updated_by=actor,
        created_at=now,
        updated_at=now,
        boundary=boundary,
    )
    built = build_template_artifacts(
        steps=steps,
        ordering=draft.ordering,
        boundary=boundary,
        runtime_defaults=runtime_defaults,
    )
    version = TemplateVersion(
        id=version_id,
        template_id=template_id,
        source_import_id=import_id,
        source_draft_id=draft_id,
        source_draft_revision=draft.revision,
        steps=steps,
        ordering=draft.ordering,
        boundary=boundary,
        runtime_defaults=runtime_defaults,
        artifacts=built.artifacts,
        sha256=built.sha256,
        published_by=actor,
        published_at=now,
    )

    session.add(TemplateImportRow.from_domain(imported))
    session.flush()
    session.add(SopTemplateRow.from_domain(template))
    session.flush()
    session.add(TemplateDraftRow.from_domain(draft))
    session.flush()
    session.add(TemplateVersionRow.from_domain(version))
    session.flush()
    return TemplateFixture(
        version_id=version_id,
        template_id=template_id,
        import_id=import_id,
        draft_id=draft_id,
        station_id=owned_station_id,
    )


def remove_template_versions(
    session: Connection | Session, fixtures: tuple[TemplateFixture, ...]
) -> None:
    """Remove committed fixtures while respecting the immutable-version trigger."""
    if not fixtures:
        return
    version_ids = [fixture.version_id for fixture in fixtures]
    draft_ids = [fixture.draft_id for fixture in fixtures]
    template_ids = [fixture.template_id for fixture in fixtures]
    import_ids = [fixture.import_id for fixture in fixtures]
    owned_station_ids = [
        fixture.station_id for fixture in fixtures if fixture.station_id is not None
    ]
    session.execute(text("ALTER TABLE template_version DISABLE TRIGGER template_version_immutable"))
    try:
        session.execute(delete(TemplateVersionRow).where(TemplateVersionRow.id.in_(version_ids)))
    finally:
        session.execute(
            text("ALTER TABLE template_version ENABLE TRIGGER template_version_immutable")
        )
    session.execute(delete(TemplateDraftRow).where(TemplateDraftRow.id.in_(draft_ids)))
    session.execute(delete(SopTemplateRow).where(SopTemplateRow.id.in_(template_ids)))
    session.execute(delete(TemplateImportRow).where(TemplateImportRow.id.in_(import_ids)))
    if owned_station_ids:
        session.execute(delete(StationRow).where(StationRow.id.in_(owned_station_ids)))
