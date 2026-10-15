"""S044: 真实 PostgreSQL + Edge SQLite/临时目录的删除、保护及结果重放。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

from edge_runtime.evidence_deletion import EvidenceDeletionExecutor, EvidenceDeletionTransportError
from edge_runtime.evidence_media import evidence_artifact_directory, evidence_media_identity
from edge_runtime.judgment.evidence import EvidenceClip
from edge_runtime.judgment.model import (
    Decision,
    EvidenceSpan,
    HostInstant,
    Instance,
    JudgmentState,
    Lifecycle,
    Ordering,
    RuntimeParameters,
    Template,
)
from edge_runtime.judgment.reasons import Verdict
from edge_runtime.local_state import LocalState, open_local_state
from sqlalchemy import Engine, text
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
from factory_sop.evidence.model import (
    EvidenceKind,
    EvidenceOrigin,
    EvidenceReference,
    EvidenceRegistration,
    EvidenceStatus,
)
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


def _local_media(state: LocalState, root: Path, station_id: str, host_id: str) -> tuple[Path, Path]:
    station = state.station(station_id)
    moment = HostInstant(100)
    station.commit(
        state=JudgmentState(
            template=Template(steps=("step",), ordering=Ordering.ORDERED, start_signal="step"),
            parameters=RuntimeParameters(idle_timeout=300, step_deadline=60),
        ),
        decisions=(
            Decision(
                instance_id=1,
                verdict=Verdict.PASS,
                reasons=(),
                violations=(),
                lifecycle=Lifecycle.CLOSED_BY_COMPLETE_SET,
                evidence=EvidenceSpan.at(moment),
            ),
        ),
        evidence=(EvidenceClip(1, moment, HostInstant(95), HostInstant(105)),),
        closed_instances=(Instance(1, moment, moment),),
        report_provenance={},
        latched_at="2026-01-05T00:00:00Z",
        latched_monotonic=moment,
    )
    (pending,) = station.pending_evidence()
    artifact = root / evidence_artifact_directory(
        evidence_media_identity(host_id, pending.queue_id), 95, 105
    )
    artifact.mkdir(parents=True)
    clip, still = artifact / "camera.mp4", artifact / "camera.jpg"
    clip.write_bytes(b"synthetic evidence clip")
    still.write_bytes(b"synthetic evidence keyframe")
    media = json.dumps(
        [
            {
                "media_path": "camera",
                "clip_file": clip.name,
                "keyframe_file": still.name,
                "material_generation": "original",
                "segment_generations": {"original": 1},
            }
        ]
    )
    station.record_evidence_slice(
        pending.queue_id, at=HostInstant(110), media_results=media, covered_from=95, covered_to=105
    )
    station.mark_evidence_registered(
        pending.queue_id, at=HostInstant(120), expected_media_results=media
    )
    return clip, still


def test_real_owners_keep_review_media_and_replay_deleted_feedback(
    engine: Engine,
    runtime_topology: RuntimeTopology,
    tmp_path: Path,
) -> None:
    topology = runtime_topology
    host, station = str(topology.host.id), str(topology.station.id)
    root = tmp_path / "media"
    state = open_local_state(str(tmp_path / "local.sqlite"))
    identifiers: list[str] = []
    try:
        bundle = ConfigurationBundle(host, 1, RECEIVED_AT.isoformat(), ())
        revision = _configure_current(topology, engine, digest=bundle.effective_sha256)
        bundle = replace(bundle, config_revision=revision)
        state.configuration().confirm(bundle, confirmed_at=1)
        clip, still = _local_media(state, root, station, host)
        _seed_instance(engine, topology, instance_id=1)
        with session_factory(engine)() as session:
            for path in (clip, still):
                reference = path.relative_to(root).as_posix()
                identifiers.append(reference)
                assert PostgresEvidenceRepository(session).insert(
                    EvidenceReference(
                        EvidenceRegistration(
                            evidence_id=reference,
                            host_id=host,
                            station_id=station,
                            instance_id=1,
                            violation_id=None,
                            kind=EvidenceKind.CLIP,
                            origin=EvidenceOrigin.AUTOMATIC,
                            anchor=100,
                            window_start=95,
                            window_end=105,
                            generation="original",
                            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                            size=path.stat().st_size,
                            reference=reference,
                        ),
                        EvidenceStatus.AVAILABLE,
                        None,
                        RECEIVED_AT,
                    )
                )
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
