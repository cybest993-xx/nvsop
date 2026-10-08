"""Evidence material transitions are atomic across concurrent Center requests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import UUID

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session as DatabaseSession

from factory_sop.evidence.adapters.repository import PostgresEvidenceRepository
from factory_sop.evidence.errors import EvidenceRefusal, EvidenceRefusedError
from factory_sop.evidence.model import (
    EvidenceKind,
    EvidenceOrigin,
    EvidenceReference,
    EvidenceRegistration,
)
from factory_sop.evidence.usecases import register_evidence

HOST_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f401")
STATION_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f402")
EVIDENCE_ID = "evidence-material-race"
NOW = datetime(2026, 9, 30, tzinfo=UTC)


class Owner:
    def owns_station(self, *, host_id: UUID, station_id: UUID) -> bool:
        return host_id == HOST_ID and station_id == STATION_ID


class RacingEvidenceRepository(PostgresEvidenceRepository):
    def __init__(self, session: DatabaseSession, barrier: Barrier) -> None:
        super().__init__(session)
        self._barrier = barrier
        self._first_find = True

    def find(self, evidence_id: str) -> EvidenceReference | None:
        value = super().find(evidence_id)
        if self._first_find:
            self._first_find = False
            self._barrier.wait(timeout=5)
        return value


def registration(*, sha256: str | None, reference: str | None) -> EvidenceRegistration:
    return EvidenceRegistration(
        evidence_id=EVIDENCE_ID,
        host_id=str(HOST_ID),
        station_id=str(STATION_ID),
        instance_id=7,
        violation_id="decision-7#0",
        kind=EvidenceKind.CLIP,
        origin=EvidenceOrigin.AUTOMATIC,
        anchor=100.0,
        window_start=95.0,
        window_end=105.0,
        generation="original",
        sha256=sha256,
        size=None if sha256 is None else 1024,
        reference=reference,
    )


def test_competing_available_material_cannot_overwrite_the_winner(engine: Engine) -> None:
    owner = Owner()
    try:
        with DatabaseSession(engine) as setup:
            register_evidence(
                registration(sha256=None, reference=None),
                received_at=NOW,
                evidence=PostgresEvidenceRepository(setup),
                host_gateway=owner,
            )
            setup.commit()

        barrier = Barrier(2)

        def promote(material: tuple[str, str]) -> tuple[str, str]:
            digest, reference = material
            with DatabaseSession(engine) as session:
                repository = RacingEvidenceRepository(session, barrier)
                try:
                    value = register_evidence(
                        registration(sha256=digest, reference=reference),
                        received_at=NOW,
                        evidence=repository,
                        host_gateway=owner,
                    )
                    session.commit()
                    return "available", value.registration.sha256 or ""
                except EvidenceRefusedError as error:
                    session.rollback()
                    return error.code.value, ""

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(
                workers.map(
                    promote,
                    (("a" * 64, "evidence/a.mp4"), ("b" * 64, "evidence/b.mp4")),
                )
            )

        assert {result[0] for result in results} == {
            EvidenceRefusal.IDENTITY_CONFLICT.value,
            "available",
        }
        winner = next(result[1] for result in results if result[0] == "available")
        with DatabaseSession(engine) as verify:
            stored = PostgresEvidenceRepository(verify).find(EVIDENCE_ID)
            assert stored is not None
            assert stored.registration.sha256 == winner
            assert stored.registration.reference in {"evidence/a.mp4", "evidence/b.mp4"}
    finally:
        with engine.begin() as cleanup:
            cleanup.execute(
                text("DELETE FROM evidence_evidence WHERE evidence_id = :evidence_id"),
                {"evidence_id": EVIDENCE_ID},
            )
