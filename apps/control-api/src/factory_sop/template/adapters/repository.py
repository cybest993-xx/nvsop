"""模板模块的 PostgreSQL 仓储适配器。"""

from __future__ import annotations

from typing import Any, NoReturn, cast
from uuid import UUID

from sqlalchemy import CursorResult, and_, func, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session as DatabaseSession
from sqlalchemy.sql.elements import ColumnElement

from factory_sop.template.adapters.tables import (
    SopTemplateRow,
    TemplateConfigurationReportRow,
    TemplateDraftRow,
    TemplateImportRow,
    TemplateStationBindingRow,
    TemplateVersionRow,
)
from factory_sop.template.errors import TemplateRefusalCode, TemplateRefusedError
from factory_sop.template.model import (
    SopTemplate,
    TemplateArtifactName,
    TemplateConfigurationReport,
    TemplateDraft,
    TemplateDraftDocument,
    TemplateImport,
    TemplateStationBinding,
    TemplateVersion,
    TemplateVersionArtifact,
    TemplateVersionWriteResult,
)


class PostgresTemplateRepository:
    """通过请求事务访问 `template_*` 行；任何方法都不提交事务。"""

    def __init__(self, session: DatabaseSession) -> None:
        self._session = session

    def add_import(self, record: TemplateImport) -> None:
        self._session.add(TemplateImportRow.from_domain(record))
        self._flush()

    def add_template(self, template: SopTemplate) -> None:
        self._session.add(SopTemplateRow.from_domain(template))
        self._flush()

    def template_by_id(self, template_id: UUID) -> SopTemplate | None:
        row = self._session.get(SopTemplateRow, template_id)
        return row.to_domain() if row is not None else None

    def add_draft(self, draft: TemplateDraft) -> None:
        self._session.add(TemplateDraftRow.from_domain(draft))
        self._flush()

    def import_by_id(self, import_id: UUID) -> TemplateImport | None:
        row = self._session.get(TemplateImportRow, import_id)
        return row.to_domain() if row is not None else None

    def draft_by_id(self, draft_id: UUID) -> TemplateDraftDocument | None:
        return self._draft_document(self._session.get(TemplateDraftRow, draft_id))

    def draft_for_publish(self, draft_id: UUID) -> TemplateDraftDocument | None:
        draft_row = self._session.scalar(
            select(TemplateDraftRow).where(TemplateDraftRow.id == draft_id).with_for_update()
        )
        return self._draft_document(draft_row)

    def _draft_document(self, draft_row: TemplateDraftRow | None) -> TemplateDraftDocument | None:
        if draft_row is None:
            return None
        template_row = self._session.get(SopTemplateRow, draft_row.template_id)
        if template_row is None:
            return None
        return TemplateDraftDocument(template=template_row.to_domain(), draft=draft_row.to_domain())

    def save_draft(self, draft: TemplateDraft, *, expected_revision: int) -> None:
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(TemplateDraftRow)
                .where(
                    TemplateDraftRow.id == draft.id,
                    TemplateDraftRow.revision == expected_revision,
                )
                .values(
                    steps=[step.to_wire() for step in draft.steps],
                    ordering=draft.ordering,
                    runtime_defaults=draft.runtime_defaults.to_wire(),
                    boundary=draft.boundary.to_wire() if draft.boundary is not None else None,
                    revision=draft.revision,
                    updated_by=draft.updated_by,
                    updated_at=draft.updated_at,
                )
            ),
        )
        if result.rowcount == 0:
            row = self._session.get(TemplateDraftRow, draft.id)
            if row is None:
                raise TemplateRefusedError(TemplateRefusalCode.DRAFT_NOT_FOUND)
            raise TemplateRefusedError(TemplateRefusalCode.STALE_REVISION)

    def page_drafts(self, *, page: int, page_size: int) -> tuple[list[TemplateDraftDocument], int]:
        total = cast(
            "int", self._session.scalar(select(func.count()).select_from(TemplateDraftRow))
        )
        rows = self._session.execute(
            select(TemplateDraftRow, SopTemplateRow)
            .join(SopTemplateRow, SopTemplateRow.id == TemplateDraftRow.template_id)
            .order_by(TemplateDraftRow.created_at.desc(), TemplateDraftRow.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return [
            TemplateDraftDocument(template=template.to_domain(), draft=draft.to_domain())
            for draft, template in rows
        ], total

    def page_imports(self, *, page: int, page_size: int) -> tuple[list[TemplateImport], int]:
        total = cast(
            "int", self._session.scalar(select(func.count()).select_from(TemplateImportRow))
        )
        rows = self._session.scalars(
            select(TemplateImportRow)
            .order_by(TemplateImportRow.imported_at.desc(), TemplateImportRow.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return [row.to_domain() for row in rows], total

    def binding_by_station(self, station_id: UUID) -> TemplateStationBinding | None:
        row = self._session.scalar(
            select(TemplateStationBindingRow).where(
                TemplateStationBindingRow.station_id == station_id
            )
        )
        return row.to_domain() if row is not None else None

    def save_binding(
        self,
        binding: TemplateStationBinding,
        *,
        expected_revision: int | None = None,
    ) -> None:
        values = TemplateStationBindingRow.values_from_domain(binding)
        if expected_revision is None:
            try:
                result = cast(
                    "CursorResult[Any]",
                    self._session.execute(
                        postgres_insert(TemplateStationBindingRow)
                        .values(values)
                        .on_conflict_do_nothing(
                            index_elements=[TemplateStationBindingRow.station_id]
                        )
                        .returning(TemplateStationBindingRow.id)
                    ),
                )
            except DatabaseError as error:
                _refuse_template_constraint(error)
            if result.scalar_one_or_none() is None:
                raise TemplateRefusedError(TemplateRefusalCode.BINDING_STATION_TAKEN)
            return

        try:
            result = cast(
                "CursorResult[Any]",
                self._session.execute(
                    update(TemplateStationBindingRow)
                    .where(
                        TemplateStationBindingRow.id == binding.id,
                        TemplateStationBindingRow.station_id == binding.station_id,
                        TemplateStationBindingRow.revision == expected_revision,
                    )
                    .values(
                        desired_version_id=binding.desired_version_id,
                        desired_sha256=binding.desired_sha256,
                        desired_config_revision=binding.desired_config_revision,
                        revision=binding.revision,
                        updated_by=binding.updated_by,
                        updated_at=binding.updated_at,
                    )
                ),
            )
        except DatabaseError as error:
            _refuse_template_constraint(error)
        if result.rowcount == 0:
            row = self._session.scalar(
                select(TemplateStationBindingRow).where(
                    TemplateStationBindingRow.id == binding.id,
                    TemplateStationBindingRow.station_id == binding.station_id,
                )
            )
            if row is None:
                raise TemplateRefusedError(TemplateRefusalCode.BINDING_NOT_FOUND)
            raise TemplateRefusedError(TemplateRefusalCode.STALE_REVISION)

    def report_by_backend(
        self, station_id: UUID, backend_id: UUID
    ) -> TemplateConfigurationReport | None:
        row = self._session.scalar(
            select(TemplateConfigurationReportRow).where(
                TemplateConfigurationReportRow.station_id == station_id,
                TemplateConfigurationReportRow.backend_id == backend_id,
            )
        )
        return row.to_domain() if row is not None else None

    def reports_for_station(self, station_id: UUID) -> list[TemplateConfigurationReport]:
        rows = self._session.scalars(
            select(TemplateConfigurationReportRow)
            .where(TemplateConfigurationReportRow.station_id == station_id)
            .order_by(TemplateConfigurationReportRow.backend_id)
        ).all()
        return [row.to_domain() for row in rows]

    def save_report(
        self,
        report: TemplateConfigurationReport,
        *,
        expected: TemplateConfigurationReport | None = None,
    ) -> None:
        """按期望快照条件写入，防止并发上报覆盖较新的事实。"""
        values = TemplateConfigurationReportRow.values_from_domain(report)
        conflict_key = [
            TemplateConfigurationReportRow.station_id,
            TemplateConfigurationReportRow.backend_id,
        ]
        try:
            if expected is None:
                result = cast(
                    "CursorResult[Any]",
                    self._session.execute(
                        postgres_insert(TemplateConfigurationReportRow)
                        .values(values)
                        .on_conflict_do_nothing(index_elements=conflict_key)
                        .returning(TemplateConfigurationReportRow.station_id)
                    ),
                )
                if result.scalar_one_or_none() is None:
                    raise TemplateRefusedError(TemplateRefusalCode.REPORT_CONFLICT)
                return

            update_values = {
                key: value
                for key, value in values.items()
                if key not in {"station_id", "backend_id", "created_by", "created_at"}
                and (key not in {"updated_by", "updated_at"} or value is not None)
            }
            result = cast(
                "CursorResult[Any]",
                self._session.execute(
                    update(TemplateConfigurationReportRow)
                    .where(
                        TemplateConfigurationReportRow.station_id == expected.station_id,
                        TemplateConfigurationReportRow.backend_id == expected.backend_id,
                        _report_matches(expected),
                    )
                    .values(**update_values)
                ),
            )
            if result.rowcount == 0:
                raise TemplateRefusedError(TemplateRefusalCode.REPORT_CONFLICT)
        except DatabaseError as error:
            _refuse_template_constraint(error)

    def version_by_source(self, *, draft_id: UUID, revision: int) -> TemplateVersion | None:
        row = self._session.scalar(
            select(TemplateVersionRow).where(
                TemplateVersionRow.source_draft_id == draft_id,
                TemplateVersionRow.source_draft_revision == revision,
            )
        )
        return row.to_domain() if row is not None else None

    def add_version(self, version: TemplateVersion) -> TemplateVersionWriteResult:
        values = TemplateVersionRow.values_from_domain(version)
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                postgres_insert(TemplateVersionRow)
                .values(values)
                .on_conflict_do_nothing(constraint="uq_template_version_source_draft_revision")
                .returning(TemplateVersionRow.id)
            ),
        )
        if result.scalar_one_or_none() is None:
            existing = self.version_by_source(
                draft_id=version.source_draft_id,
                revision=version.source_draft_revision,
            )
            if existing is None:
                raise RuntimeError("template version conflict disappeared before it could be read")
            return TemplateVersionWriteResult(version=existing, created=False)
        return TemplateVersionWriteResult(version=version, created=True)

    def version_by_id(self, version_id: UUID) -> TemplateVersion | None:
        row = self._session.get(TemplateVersionRow, version_id)
        return row.to_domain() if row is not None else None

    def page_versions(self, *, page: int, page_size: int) -> tuple[list[TemplateVersion], int]:
        total = cast(
            "int", self._session.scalar(select(func.count()).select_from(TemplateVersionRow))
        )
        rows = self._session.scalars(
            select(TemplateVersionRow)
            .order_by(TemplateVersionRow.published_at.desc(), TemplateVersionRow.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return [row.to_domain() for row in rows], total

    def artifact_by_name(
        self, *, version_id: UUID, name: TemplateArtifactName
    ) -> TemplateVersionArtifact | None:
        version = self.version_by_id(version_id)
        if version is None:
            return None
        return next((artifact for artifact in version.artifacts if artifact.name is name), None)

    def _flush(self) -> None:
        try:
            self._session.flush()
        except DatabaseError:
            # 用例在写入前校验引用；未知数据库错误必须继续抛出，不能误报为模板拒绝。
            raise


def _report_matches(expected: TemplateConfigurationReport) -> ColumnElement[bool]:
    """用 NULL-safe 比较把读取快照变成条件写入的并发令牌。"""
    row = TemplateConfigurationReportRow
    return and_(
        row.host_id.is_not_distinct_from(expected.host_id),
        row.reported_version_id.is_not_distinct_from(expected.reported_version_id),
        row.reported_sha256.is_not_distinct_from(expected.reported_sha256),
        row.reported_config_revision.is_not_distinct_from(expected.reported_config_revision),
        row.reported_at.is_not_distinct_from(expected.reported_at),
        row.last_rejection_code.is_not_distinct_from(expected.last_rejection_code),
        row.last_rejection_detail.is_not_distinct_from(expected.last_rejection_detail),
        row.last_rejection_at.is_not_distinct_from(expected.last_rejection_at),
        row.updated_by.is_not_distinct_from(expected.updated_by),
        row.updated_at.is_not_distinct_from(expected.updated_at),
    )


def _refuse_template_constraint(error: DatabaseError) -> NoReturn:
    """把模板仓储能判定的唯一冲突转换为稳定拒绝。"""
    diagnostics = getattr(error.orig, "diag", None)
    constraint_name = getattr(diagnostics, "constraint_name", None)
    if constraint_name is None:
        text = str(error.orig)
        if "uq_template_station_binding_station_id" in text:
            constraint_name = "uq_template_station_binding_station_id"
    if constraint_name == "uq_template_station_binding_station_id":
        raise TemplateRefusedError(TemplateRefusalCode.BINDING_STATION_TAKEN) from error
    raise error
