"""S044: 真实 PostgreSQL + Edge SQLite/临时目录的删除、保护及结果重放。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

from edge_runtime.evidence_deletion import (  # type: ignore[import-untyped]
    EvidenceDeletionExecutor,
    EvidenceDeletionTransportError,
)
from edge_runtime.local_state import open_local_state  # type: ignore[import-untyped]
from sqlalchemy import Engine, text
from test_evidence_deletion_bridge import _seed_local_and_register
from test_evidence_deletion_protection import (
    NOW,
    RECEIVED_AT,
    SECRET,
    TTL_SECONDS,
    _caller,
    _configure_current,
    _seams,
    _seed_instance,
)
from test_retention_sweep_postgres import _run_sweep
from test_runtime_observation_http import RuntimeTopology
from test_runtime_observation_http import runtime_topology as runtime_topology

from factory_sop.evidence.adapters.repository import (
    PostgresEvidenceRepository,
    PostgresEvidenceReviewRepository,
)
from factory_sop.evidence.model import EvidenceStatus
from factory_sop.evidence.reviews import begin_review
from factory_sop.persistence import session_factory
from factory_sop.retention.deletion import (
    claim_evidence_deletion,
    settle_evidence_deletion,
    verify_evidence_deletion,
)
from nvsop_contracts import (
    ConfigurationBundle,
    EvidenceDeletionRequest,
    EvidenceDeletionResponse,
    EvidenceDeletionResultRequest,
    EvidenceDeletionResultResponse,
    EvidenceDeletionVerifyRequest,
    EvidenceDeletionVerifyResponse,
)


class _PostgresTransport:
    """仅替换网络传输；资格、保护、认领和结案均调用真实中心用例与 owner。"""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.result_unreachable = True

    def claim(self, request: EvidenceDeletionRequest) -> EvidenceDeletionResponse:
        with session_factory(self.engine)() as session:
            seams = _seams(session)
            result = claim_evidence_deletion(
                host_id=UUID(request.host_id),
                config_revision=request.config_revision,
                effective_sha256=request.effective_sha256,
                candidates=request.candidates,
                evidence=seams.evidence,
                monitor=seams.monitor,
                stations=seams.stations,
                sources=seams.sources,
                policies=seams.policies,
                overrides=seams.overrides,
                now=NOW,
                ticket_ttl_seconds=TTL_SECONDS,
                secret=SECRET,
            )
            session.commit()
            return result

    def verify(self, request: EvidenceDeletionVerifyRequest) -> EvidenceDeletionVerifyResponse:
        with session_factory(self.engine)() as session:
            seams = _seams(session)
            result = verify_evidence_deletion(
                host_id=UUID(request.host_id),
                ticket=request.ticket,
                config_revision=request.config_revision,
                effective_sha256=request.effective_sha256,
                candidate=request.candidate,
                evidence=seams.evidence,
                monitor=seams.monitor,
                stations=seams.stations,
                sources=seams.sources,
                policies=seams.policies,
                overrides=seams.overrides,
                now=NOW,
                ticket_ttl_seconds=TTL_SECONDS,
                secret=SECRET,
            )
            session.commit()
            return result

    def report(self, request: EvidenceDeletionResultRequest) -> EvidenceDeletionResultResponse:
        if self.result_unreachable:
            raise EvidenceDeletionTransportError("synthetic result outage")
        with session_factory(self.engine)() as session:
            result = settle_evidence_deletion(
                host_id=UUID(request.host_id),
                claim_id=UUID(request.claim_id),
                evidence_id=request.evidence_id,
                outcome=request.outcome,
                reason_code=request.reason_code,
                detail=request.detail,
                evidence=_seams(session).evidence,
                now=NOW,
            )
            session.commit()
            return EvidenceDeletionResultResponse.from_wire(result.to_wire())


def test_real_owners_keep_review_media_and_replay_deleted_feedback(
    engine: Engine,
    runtime_topology: RuntimeTopology,
    tmp_path: Path,
) -> None:
    topology = runtime_topology
    host = str(topology.host.id)
    root = tmp_path / "media"
    state = open_local_state(str(tmp_path / "local.sqlite"))
    identifiers: list[str] = []
    try:
        bundle = ConfigurationBundle(host, 1, RECEIVED_AT.isoformat(), ())
        revision = _configure_current(topology, engine, digest=bundle.effective_sha256)
        bundle = replace(bundle, config_revision=revision)
        state.configuration().confirm(bundle, confirmed_at=1)
        clip, expired_id = _seed_local_and_register(engine, topology, state, root, instance_id=1)
        still, protected_id = _seed_local_and_register(engine, topology, state, root, instance_id=2)
        identifiers.extend((expired_id, protected_id))
        _seed_instance(engine, topology, instance_id=1)
        _seed_instance(engine, topology, instance_id=2)
        with session_factory(engine)() as session:
            begin_review(
                _caller(),
                review_id=uuid4(),
                evidence_id=identifiers[1],
                generation="original",
                now=NOW,
                evidence=PostgresEvidenceRepository(session),
                reviews=PostgresEvidenceReviewRepository(session),
            )
            session.commit()
        transport = _PostgresTransport(engine)
        executor = EvidenceDeletionExecutor(
            state=state,
            host_id=host,
            evidence_directory=root,
            transport=transport,
            clock=NOW.timestamp,
        )
        first = executor.drain(limit=64)
        assert {(item.outcome, item.reason) for item in first} == {
            ("skipped", "result_pending"),
            ("skipped", "review_in_progress"),
        }
        assert not clip.exists()
        assert still.exists()
        # 真实中心回收不能绕过未结案结果与在审引用来删除其依赖的记录。
        with session_factory(engine)() as session:
            swept = _run_sweep(session, now=NOW, engine=engine)
            assert swept is not None
            assert swept.records is not None
            assert swept.records.deleted == 0
            assert any(item.reason == "evidence_reference_retained" for item in swept.records.skips)
            session.commit()
        assert state.deletion_journal()[0].outcome == "deleted"
        transport.result_unreachable = False
        replay = executor.drain(limit=64)
        assert any(item.outcome == "deleted" for item in replay)
        assert state.deletion_journal() == ()
        assert still.exists()
        with session_factory(engine)() as session:
            repository = PostgresEvidenceRepository(session)
            deleted, protected = (repository.find(value) for value in identifiers)
            assert deleted is not None
            assert deleted.status is EvidenceStatus.DELETED
            assert protected is not None
            assert protected.status is EvidenceStatus.AVAILABLE
            assert PostgresEvidenceReviewRepository(session).active_reviewers(
                evidence_id=identifiers[1]
            )
    finally:
        state.close()
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM evidence_evidence WHERE evidence_id = ANY(:ids)"),
                {"ids": identifiers},
            )
            for table in ("retention_sweep", "monitor_retention_cursor", "monitor_record_archive"):
                connection.execute(text(f"DELETE FROM {table}"))
