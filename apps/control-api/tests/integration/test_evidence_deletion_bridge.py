"""#206 跨进程桥: 真实 Edge 删除执行器 → 主机签名 Center HTTP → 真实 PostgreSQL → 真实 SQLite/文件。

两侧此前各自有真实边界证明（Center 签名 HTTP+PG；Edge SQLite/文件），但没有一条把真实
`EvidenceDeletionExecutor` + `HttpEvidenceDeletionTransport` 接到真实签名 Center 路由的窄场景。
本测试只做这一条：claim → verify → 本机取消链接 → result 的 wire、结案与结果重放，以及
“源文件确证不存在”按不可用结案。不引入第二套 harness。
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import time as time_module
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import uvicorn
from edge_runtime.center_client import CenterClient  # type: ignore[import-untyped]
from edge_runtime.evidence_deletion import (  # type: ignore[import-untyped]
    EvidenceDeletionExecutor,
    EvidenceDeletionTransport,
    EvidenceDeletionTransportError,
)
from edge_runtime.evidence_deletion_transport import (  # type: ignore[import-untyped]
    HttpEvidenceDeletionTransport,
)
from edge_runtime.evidence_media import (  # type: ignore[import-untyped]
    evidence_artifact_directory,
    evidence_media_identity,
    evidence_registration_payloads,
)
from edge_runtime.judgment.evidence import EvidenceClip  # type: ignore[import-untyped]
from edge_runtime.judgment.model import (  # type: ignore[import-untyped]
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
from edge_runtime.judgment.reasons import Verdict  # type: ignore[import-untyped]
from edge_runtime.local_state import LocalState, open_local_state  # type: ignore[import-untyped]
from fastapi import FastAPI
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session as DatabaseSession
from test_auth_http import settings
from test_evidence_deletion_protection import _seed_instance
from test_runtime_observation_http import RuntimeTopology
from test_runtime_observation_http import runtime_topology as runtime_topology

from factory_sop.app import API_PREFIX, create_app
from factory_sop.evidence.adapters.repository import PostgresEvidenceRepository
from factory_sop.evidence.model import (
    EvidenceKind,
    EvidenceOrigin,
    EvidenceReference,
    EvidenceRegistration,
    EvidenceStatus,
)
from factory_sop.persistence import session_factory
from nvsop_contracts import (
    EvidenceDeletionRequest,
    EvidenceDeletionResponse,
    EvidenceDeletionResultRequest,
    EvidenceDeletionResultResponse,
    EvidenceDeletionVerifyRequest,
    EvidenceDeletionVerifyResponse,
    configuration_from_wire,
)

TTL_SECONDS = 300
RECEIVED_AT = datetime(2026, 1, 5, 1, 0, tzinfo=UTC)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _RunningCenter:
    """真实 uvicorn 承载 create_app（同一 PG engine），供 Edge 签名客户端访问。"""

    def __init__(self, app: FastAPI) -> None:
        self._port = _free_port()
        self._config = uvicorn.Config(
            app, host="127.0.0.1", port=self._port, log_level="warning", access_log=False
        )
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def __enter__(self) -> _RunningCenter:
        self._thread.start()
        deadline = time_module.monotonic() + 15
        while not self._server.started and time_module.monotonic() < deadline:
            time_module.sleep(0.05)
        if not self._server.started:
            raise AssertionError("center HTTP server did not start")
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=15)


class _FlakyReportTransport:
    """第一次 result 投递模拟网络失败；后续委托真实 HTTP 传输。"""

    def __init__(self, inner: EvidenceDeletionTransport) -> None:
        self._inner = inner
        self.failed = False

    def claim(self, request: EvidenceDeletionRequest) -> EvidenceDeletionResponse:
        return cast(EvidenceDeletionResponse, self._inner.claim(request))

    def verify(self, request: EvidenceDeletionVerifyRequest) -> EvidenceDeletionVerifyResponse:
        return cast(EvidenceDeletionVerifyResponse, self._inner.verify(request))

    def report(self, request: EvidenceDeletionResultRequest) -> EvidenceDeletionResultResponse:
        if not self.failed:
            self.failed = True
            raise EvidenceDeletionTransportError("simulated result delivery failure")
        return cast(EvidenceDeletionResultResponse, self._inner.report(request))


class _RemovingVerifyTransport:
    """真实 verify 成功返回后移除源文件，模拟取消链接后、结果落账前崩溃。"""

    def __init__(self, inner: EvidenceDeletionTransport, path: Path) -> None:
        self._inner = inner
        self._path = path

    def claim(self, request: EvidenceDeletionRequest) -> EvidenceDeletionResponse:
        return cast(EvidenceDeletionResponse, self._inner.claim(request))

    def verify(self, request: EvidenceDeletionVerifyRequest) -> EvidenceDeletionVerifyResponse:
        verified = cast(EvidenceDeletionVerifyResponse, self._inner.verify(request))
        self._path.unlink(missing_ok=True)
        return verified

    def report(self, request: EvidenceDeletionResultRequest) -> EvidenceDeletionResultResponse:
        return cast(EvidenceDeletionResultResponse, self._inner.report(request))


def _empty_state() -> JudgmentState:
    return JudgmentState(
        template=Template(
            steps=("(1) step",),
            ordering=Ordering.ORDERED,
            start_signal="(1) step",
            end_signals=(),
        ),
        parameters=RuntimeParameters(idle_timeout=300.0, step_deadline=60.0),
    )


def _seed_local_and_register(
    engine: Engine,
    topology: RuntimeTopology,
    state: LocalState,
    directory: Path,
    *,
    instance_id: int,
) -> tuple[Path, str]:
    """本机真实 SQLite 证据与切片元数据, 并在 Center 登记同一条引用; 返回文件与 evidence_id。"""
    host_id, station_id = str(topology.host.id), str(topology.station.id)
    station = state.station(station_id)
    now = HostInstant(100.0)
    station.commit(
        state=_empty_state(),
        decisions=(
            Decision(
                instance_id=instance_id,
                verdict=Verdict.PASS,
                reasons=(),
                violations=(),
                lifecycle=Lifecycle.CLOSED_BY_COMPLETE_SET,
                evidence=EvidenceSpan.at(now),
            ),
        ),
        evidence=(EvidenceClip(instance_id, now, HostInstant(95.0), HostInstant(105.0)),),
        closed_instances=(Instance(instance_id, now, now),),
        report_provenance={},
        latched_at="2026-10-09T00:00:00+00:00",
        latched_monotonic=HostInstant(1),
    )
    (pending,) = station.pending_evidence()
    identity = evidence_media_identity(host_id, pending.queue_id)
    artifact = directory / evidence_artifact_directory(identity, 95.0, 105.0)
    artifact.mkdir(parents=True)
    clip = artifact / "camera-a.mp4"
    clip.write_bytes(b"bridge evidence bytes")
    (artifact / "camera-a.jpg").write_bytes(b"bridge keyframe")
    media_json = json.dumps(
        [
            {
                "media_path": "camera-a",
                "clip_file": clip.name,
                "keyframe_file": "camera-a.jpg",
                "actual_from": 95.0,
                "actual_to": 105.0,
                "material_generation": "original",
                "segment_generations": {"original": 1},
                "sha256": hashlib.sha256(clip.read_bytes()).hexdigest(),
                "size_bytes": clip.stat().st_size,
            }
        ],
        separators=(",", ":"),
    )
    station.record_evidence_slice(
        pending.queue_id,
        at=HostInstant(110),
        media_results=media_json,
        covered_from=95.0,
        covered_to=105.0,
    )
    (finalized,) = station.pending_evidence()
    payloads = evidence_registration_payloads(
        finalized, host_id=host_id, station_id=station_id, directory=directory
    )
    # 只登记片段并移除关键帧文件：该场景聚焦一条真实引用的 wire/结案/重放。
    (artifact / "camera-a.jpg").unlink()
    with session_factory(engine)() as session:
        repository = PostgresEvidenceRepository(session)
        for payload in payloads:
            if payload["kind"] != "clip":
                continue
            repository.insert(
                EvidenceReference(
                    EvidenceRegistration(
                        evidence_id=cast(str, payload["evidence_id"]),
                        host_id=cast(str, payload["host_id"]),
                        station_id=cast(str, payload["station_id"]),
                        instance_id=cast(int, payload["instance_id"]),
                        violation_id=None,
                        kind=EvidenceKind.CLIP,
                        origin=EvidenceOrigin.AUTOMATIC,
                        anchor=cast(float, payload["anchor"]),
                        window_start=cast(float, payload["window_start"]),
                        window_end=cast(float, payload["window_end"]),
                        generation=cast(str, payload["generation"]),
                        sha256=cast(str, payload["sha256"]),
                        size=cast(int, payload["size"]),
                        reference=cast(str, payload["reference"]),
                        wall_offset=cast(float, payload.get("wall_offset")),
                        required_from=cast(float, payload.get("required_from")),
                        required_to=cast(float, payload.get("required_to")),
                    ),
                    EvidenceStatus.AVAILABLE,
                    None,
                    RECEIVED_AT,
                )
            )
        session.commit()
    station.mark_evidence_registered(
        pending.queue_id, at=HostInstant(120), expected_media_results=media_json
    )
    return clip, f"{artifact.name}/{clip.name}"


def _center_client(topology: RuntimeTopology, url: str) -> CenterClient:
    return CenterClient(
        center_url=url,
        host_id=str(topology.host.id),
        host_private_key=topology.identity.private_key,
        timeout=10,
    )


def _pull_and_confirm(client: CenterClient, state: LocalState, topology: RuntimeTopology) -> None:
    path = f"{API_PREFIX}/inference-hosts/{topology.host.id}/configuration"
    response = client.get(path)
    assert response.status == 200, response.body
    bundle = configuration_from_wire(cast(dict[str, object], json.loads(response.body)))
    state.configuration().confirm(bundle, confirmed_at=time_module.time())


def _clear(engine: Engine, *evidence_ids: str) -> None:
    with engine.begin() as connection:
        if evidence_ids:
            connection.execute(
                text("DELETE FROM evidence_evidence WHERE evidence_id = ANY(:ids)"),
                {"ids": list(evidence_ids)},
            )


def test_bridge_deletes_through_signed_center_http_and_replays_result(
    engine: Engine, runtime_topology: RuntimeTopology, tmp_path: Path
) -> None:
    topology = runtime_topology
    app = create_app(
        settings().model_copy(update={"evidence_deletion_ticket_ttl_seconds": TTL_SECONDS}),
        session_factory=session_factory(engine),
    )
    evidence_id = ""
    with _RunningCenter(app) as center:
        client = _center_client(topology, center.url)
        state = open_local_state(str(tmp_path / "edge-state.sqlite"))
        try:
            _seed_instance(engine, topology, instance_id=1)
            directory = tmp_path / "evidence"
            clip, evidence_id = _seed_local_and_register(
                engine, topology, state, directory, instance_id=1
            )
            _pull_and_confirm(client, state, topology)
            transport = _FlakyReportTransport(
                HttpEvidenceDeletionTransport(client=client, host_id=str(topology.host.id))
            )
            executor = EvidenceDeletionExecutor(
                state=state,
                host_id=str(topology.host.id),
                evidence_directory=directory,
                transport=transport,
            )
            # 第一次 drain: 真实 claim/verify 成功、文件已删, 但 result 投递失败 -> 账本保留。
            first = executor.drain()
            assert [result.reason for result in first] == ["result_pending"]
            assert not clip.exists()
            journal = state.deletion_journal()
            assert [(row.stage, row.outcome) for row in journal] == [("result_pending", "deleted")]
            # 第二次 drain: 重放同一 result; Center 结案 DELETED, 账本清空。
            second = executor.drain()
            assert [(result.outcome, result.reason) for result in second] == [
                ("deleted", "retention_expired")
            ]
            assert state.deletion_journal() == ()
            with DatabaseSession(engine) as session:
                stored = PostgresEvidenceRepository(session).find(evidence_id)
                assert stored is not None
                assert stored.status is EvidenceStatus.DELETED
        finally:
            state.close()
            _clear(engine, evidence_id)


def test_bridge_reports_missing_source_as_unavailable_not_deleted(
    engine: Engine, runtime_topology: RuntimeTopology, tmp_path: Path
) -> None:
    topology = runtime_topology
    app = create_app(
        settings().model_copy(update={"evidence_deletion_ticket_ttl_seconds": TTL_SECONDS}),
        session_factory=session_factory(engine),
    )
    evidence_id = ""
    with _RunningCenter(app) as center:
        client = _center_client(topology, center.url)
        state = open_local_state(str(tmp_path / "edge-state.sqlite"))
        try:
            _seed_instance(engine, topology, instance_id=2)
            directory = tmp_path / "evidence"
            clip, evidence_id = _seed_local_and_register(
                engine, topology, state, directory, instance_id=2
            )
            _pull_and_confirm(client, state, topology)
            transport = _RemovingVerifyTransport(
                HttpEvidenceDeletionTransport(client=client, host_id=str(topology.host.id)),
                clip,
            )
            executor = EvidenceDeletionExecutor(
                state=state,
                host_id=str(topology.host.id),
                evidence_directory=directory,
                transport=transport,
            )
            results = executor.drain()
            # 源文件确证不存在: 终局为 missing, 不是 deleted; Center 记为不可用而非已删除。
            assert [(result.outcome, result.reason) for result in results] == [
                ("missing", "source_missing")
            ]
            with DatabaseSession(engine) as session:
                stored = PostgresEvidenceRepository(session).find(evidence_id)
                assert stored is not None
                assert stored.status is EvidenceStatus.FAILED
                assert stored.failure_reason == "source_missing"
            assert state.deletion_journal() == ()
        finally:
            state.close()
            _clear(engine, evidence_id)
