"""The PostgreSQL side of the host and backend-history repository seams.

No method commits. One request is one transaction, opened and committed by the HTTP adapter
layer (ADR-0002). Backend rows are mapped here because the host's foreign key and historical
reference invariant need real storage in phase one; backend CRUD, connection tests, and model
observation writers are intentionally deferred to phase two.
"""

from __future__ import annotations

from typing import Any, NoReturn, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, exists, func, select, update
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.device.adapters.tables import InferenceBackendRow, InferenceHostRow
from factory_sop.device.errors import DeviceRefusalCode, DeviceRefusedError
from factory_sop.device.model import InferenceHost

# What each unique-constraint violation means, named by the metadata convention
# (factory_sop/persistence.py). The backend foreign key is translated only when a host is
# deleted; backend insert/update meanings belong to the second-phase backend use cases.
_CONSTRAINT_REFUSALS: dict[str, DeviceRefusalCode] = {
    "uq_device_inference_host_name": DeviceRefusalCode.INFERENCE_HOST_NAME_TAKEN,
}
_BACKEND_FOREIGN_KEY = "fk_device_inference_backend_host_id_device_inference_host"
_TRIGGERED_HOST_DEACTIVATED = "device_backend_host_deactivated"


def _refuse_constraint_violation(
    error: DatabaseError,
    *,
    foreign_key_to_host: DeviceRefusalCode | None = None,
) -> NoReturn:
    """Translate the host-side constraints, or let an unrelated database error surface."""
    original = error.orig
    diagnostics = getattr(original, "diag", None)
    constraint_name = getattr(diagnostics, "constraint_name", None)
    sqlstate = getattr(original, "sqlstate", None)
    message = getattr(diagnostics, "message_primary", None)
    if sqlstate == "P0001" and message == _TRIGGERED_HOST_DEACTIVATED:
        raise DeviceRefusedError(DeviceRefusalCode.INFERENCE_HOST_DEACTIVATED) from error
    if constraint_name == _BACKEND_FOREIGN_KEY:
        if foreign_key_to_host is None:
            raise error
        raise DeviceRefusedError(foreign_key_to_host) from error
    refusal = _CONSTRAINT_REFUSALS.get(constraint_name or "")
    if refusal is None:
        raise error
    raise DeviceRefusedError(refusal) from error


class PostgresInferenceHostRepository:
    """`device_inference_host` through the request's session."""

    def __init__(self, session: DatabaseSession) -> None:
        self._session = session

    def add(self, host: InferenceHost) -> None:
        self._session.add(InferenceHostRow.from_domain(host))
        try:
            self._session.flush()
        except DatabaseError as error:
            _refuse_constraint_violation(error)

    def save(self, host: InferenceHost, *, expected_revision: int) -> None:
        values = {
            "name": host.name,
            "address": host.address,
            "mediamtx_address": host.mediamtx_address,
            "recording_window_seconds": host.recording_window_seconds,
            "disk_watermark_percent": host.disk_watermark_percent,
            "status": host.status,
            "revision": host.revision,
            "updated_by": host.updated_by,
            "updated_at": host.updated_at,
        }
        try:
            result = cast(
                "CursorResult[Any]",
                self._session.execute(
                    update(InferenceHostRow)
                    .where(
                        InferenceHostRow.id == host.id,
                        InferenceHostRow.revision == expected_revision,
                    )
                    .values(**values)
                ),
            )
        except DatabaseError as error:
            _refuse_constraint_violation(error)
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.scalar(
                    select(InferenceHostRow)
                    .where(InferenceHostRow.id == host.id)
                    .execution_options(populate_existing=True)
                ),
            )

    def by_id(self, host_id: UUID) -> InferenceHost | None:
        row = self._session.get(InferenceHostRow, host_id)
        return row.to_domain() if row is not None else None

    def remove(self, host_id: UUID, *, expected_revision: int) -> bool:
        try:
            result = cast(
                "CursorResult[Any]",
                self._session.execute(
                    delete(InferenceHostRow).where(
                        InferenceHostRow.id == host_id,
                        InferenceHostRow.revision == expected_revision,
                    )
                ),
            )
        except DatabaseError as error:
            # A backend row keeps the historical reference. This is the database backstop for
            # `delete_host`, which checks the same invariant through `any_for_host` first.
            _refuse_constraint_violation(
                error,
                foreign_key_to_host=DeviceRefusalCode.INFERENCE_HOST_HAS_BACKENDS,
            )
        if result.rowcount == 0:
            _refuse_lost_race(
                missing_refusal=DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND,
                present_refusal=DeviceRefusalCode.STALE_REVISION,
                row=self._session.scalar(
                    select(InferenceHostRow)
                    .where(InferenceHostRow.id == host_id)
                    .execution_options(populate_existing=True)
                ),
            )
        return True

    def page_of(self, *, page: int, page_size: int) -> tuple[list[InferenceHost], int]:
        total = cast(
            "int", self._session.scalar(select(func.count()).select_from(InferenceHostRow))
        )
        rows = self._session.scalars(
            select(InferenceHostRow)
            .order_by(InferenceHostRow.created_at.desc(), InferenceHostRow.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return [row.to_domain() for row in rows], total


class PostgresInferenceBackendRepository:
    """The phase-one read needed to protect a host's stored backend history."""

    def __init__(self, session: DatabaseSession) -> None:
        self._session = session

    def any_for_host(self, host_id: UUID) -> bool:
        """Tell host deletion whether the shared backend table still references the host."""
        return bool(
            self._session.scalar(select(exists().where(InferenceBackendRow.host_id == host_id)))
        )


def _refuse_lost_race(
    *,
    missing_refusal: DeviceRefusalCode,
    present_refusal: DeviceRefusalCode,
    row: InferenceHostRow | None,
) -> NoReturn:
    """Say which way the conditional update lost: the row moved, or the row is gone."""
    if row is None:
        raise DeviceRefusedError(missing_refusal)
    raise DeviceRefusedError(present_refusal)
