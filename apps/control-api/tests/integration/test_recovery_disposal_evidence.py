"""S134: Edge SQLite、本机证据文件、签名 HTTP 与 Center PostgreSQL 的恢复对账。"""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from _integration_support import client_for, settings_for
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session as DatabaseSession
from test_runtime_observation_http import (
    RuntimeTopology,
    _host_headers,
)
from test_runtime_observation_http import (
    runtime_topology as runtime_topology,
)

from factory_sop.evidence.adapters.repository import PostgresEvidenceRepository
from nvsop_contracts import ReportedDisposal


def _edge_module(name: str) -> ModuleType:
    """现有 Center 集成测试按需加载 Edge 源码，保留两个应用独立的包路径。"""
    source = str(Path(__file__).resolve().parents[4] / "apps/edge-runtime/src")
    sys.path.insert(0, source)
    try:
        return importlib.import_module(f"edge_runtime.{name}")
    finally:
        sys.path.remove(source)


@pytest.mark.parametrize("result_kind", ["written", "unknown"])
def test_disposal_result_only_reconciles_after_lost_ack_restart_and_protocol_rejection(
    engine: Engine,
    runtime_topology: RuntimeTopology,
    dataset_storage_root: Path,
    tmp_path: Path,
    result_kind: str,
) -> None:
    local = cast(Any, _edge_module("local_state"))
    disposal = cast(Any, _edge_module("local_state.disposal"))
    reporting = cast(Any, _edge_module("reporting"))
    model = cast(Any, _edge_module("judgment.model"))
    host_id = str(runtime_topology.host.id)
    station_id = str(runtime_topology.station.id)
    database = str(tmp_path / "local-disposal.sqlite")
    state = local.open_local_state(database)
    intent = disposal.DisposalIntent(
        station_id=station_id,
        idempotency_key="s134-executed-stop",
        connector_id=str(runtime_topology.connector.id),
        point_id=str(runtime_topology.point.id),
        actor="supervisor",
        requested_state="active",
        violation_ref="1:MISSED_STEP:step-2",
        violation_instance_id=1,
        source="station_policy:stop",
        report_host_id=host_id,
    )
    ledger = state.disposal()
    ledger.ensure_intent(intent)
    assert ledger.claim(intent, now=1.0, lease_seconds=5.0).claimed is True
    ledger.record_result(
        intent, result=disposal.StoredDisposalResult(kind=result_kind, detail=None, at=2.0)
    )
    (queue_id,) = ledger.pending_report_ids()
    event_id = f"{host_id}:disposal:{queue_id}"
    stored_result = ledger.result_for(station_id, intent.idempotency_key)
    assert stored_result is not None
    assert stored_result.kind == result_kind

    def mirror_count() -> int:
        with engine.connect() as connection:
            return int(
                connection.scalar(
                    text("SELECT count(*) FROM monitor_disposal WHERE event_id = :event_id"),
                    {"event_id": event_id},
                )
                or 0
            )

    path = "/api/v1/monitor/reported-disposals"
    try:
        assert mirror_count() == 0
        with client_for(engine, settings_for(engine, storage_root=dataset_storage_root)) as client:

            class SignedTransport:
                def __init__(self) -> None:
                    self.posts: list[dict[str, object]] = []
                    self.responses: list[int] = []
                    self.drop_first_ack = True
                    self.reject_protocol = False

                def send_disposal(self, report: ReportedDisposal) -> None:
                    original = report.to_wire()
                    body = dict(original)
                    if self.reject_protocol:
                        body["contract_version"] = cast(int, body["contract_version"]) + 1
                    response = client.post(
                        path,
                        json=body,
                        headers=_host_headers(
                            runtime_topology, method="POST", path=path, body=body
                        ),
                    )
                    self.responses.append(response.status_code)
                    if response.status_code != 200:
                        raise ValueError(f"Center refused disposal HTTP {response.status_code}")
                    self.posts.append(original)
                    if self.drop_first_ack:
                        self.drop_first_ack = False
                        raise OSError("center committed disposal but acknowledgement was lost")

            transport = SignedTransport()
            first = reporting.HostReportReconciler(
                reports=state.reports(), transport=transport
            ).flush(now=model.HostInstant(10.0), reported_at="2026-10-10T00:00:00Z")
            assert len(first) == 1
            assert first[0].sent is False
            assert mirror_count() == 1
            assert ledger.pending_report_ids() == (queue_id,)
            assert ledger.result_for(station_id, intent.idempotency_key) == stored_result
            state.close()
            state = local.open_local_state(database)
            ledger = state.disposal()
            assert ledger.pending_report_ids() == (queue_id,)
            replay_claim = ledger.claim(intent, now=20.0, lease_seconds=5.0)
            assert replay_claim.claimed is False
            assert replay_claim.result == stored_result

            transport.reject_protocol = True
            rejected = reporting.HostReportReconciler(
                reports=state.reports(), transport=transport
            ).flush(now=model.HostInstant(21.0), reported_at="2026-10-10T00:01:00Z")
            assert len(rejected) == 1
            assert rejected[0].sent is False
            assert "HTTP 422" in (rejected[0].error or "")
            assert ledger.pending_report_ids() == (queue_id,)
            assert mirror_count() == 1

            transport.reject_protocol = False
            retried = reporting.HostReportReconciler(
                reports=state.reports(), transport=transport
            ).flush(now=model.HostInstant(22.0), reported_at="2026-10-10T00:02:00Z")
            assert len(retried) == 1
            assert retried[0].sent is True
            assert transport.responses == [200, 422, 200]
            assert transport.posts[0] == transport.posts[1]
            assert transport.posts[0]["event_id"] == event_id
            assert transport.posts[0]["reported_at"] == "2026-10-10T00:00:00Z"
            assert transport.posts[0]["result_kind"] == result_kind
            assert ledger.pending_report_ids() == ()
            assert ledger.result_for(station_id, intent.idempotency_key) == stored_result
            assert mirror_count() == 1
            with engine.connect() as connection:
                saved = connection.scalar(
                    text("SELECT payload FROM monitor_disposal WHERE event_id = :event_id"),
                    {"event_id": event_id},
                )
            assert saved == transport.posts[0]
            assert (
                reporting.HostReportReconciler(reports=state.reports(), transport=transport).flush(
                    now=model.HostInstant(23.0), reported_at="2026-10-10T00:03:00Z"
                )
                == ()
            )
            assert mirror_count() == 1
    finally:
        state.close()
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM monitor_disposal WHERE event_id = :event_id"),
                {"event_id": event_id},
            )


def test_evidence_reference_reconciles_real_center_and_keeps_local_media(
    engine: Engine, runtime_topology: RuntimeTopology, dataset_storage_root: Path, tmp_path: Path
) -> None:
    local = cast(Any, _edge_module("local_state"))
    model = cast(Any, _edge_module("judgment.model"))
    reasons = cast(Any, _edge_module("judgment.reasons"))
    clips = cast(Any, _edge_module("judgment.evidence"))
    center_api = cast(Any, _edge_module("center_client"))
    evidence_reporting = cast(Any, _edge_module("evidence_reporting"))
    host_id = str(runtime_topology.host.id)
    station_id = str(runtime_topology.station.id)
    database = str(tmp_path / "local-evidence.sqlite")
    directory = tmp_path / "media"

    state = local.open_local_state(
        database,
        evidence_source=lambda _: local.EvidenceSource(wall_offset=0.0, media_paths=("camera-a",)),
    )
    now = model.HostInstant(100.0)
    station = state.station(station_id)
    station.commit(
        state=model.JudgmentState(
            template=model.Template(
                steps=("step-1",), ordering=model.Ordering.ORDERED, start_signal="step-1"
            ),
            parameters=model.RuntimeParameters(idle_timeout=30.0, step_deadline=10.0),
            next_instance_id=2,
        ),
        decisions=(
            model.Decision(
                instance_id=1,
                verdict=reasons.Verdict.PASS,
                reasons=(),
                violations=(),
                lifecycle=model.Lifecycle.CLOSED_BY_COMPLETE_SET,
                evidence=model.EvidenceSpan.at(now),
            ),
        ),
        evidence=(clips.EvidenceClip(1, now, model.HostInstant(95), model.HostInstant(105)),),
        closed_instances=(model.Instance(1, now, now),),
        report_provenance={},
        latched_at="2026-10-10T00:00:00Z",
        latched_monotonic=model.HostInstant(1.0),
    )
    (pending,) = station.pending_evidence()
    artifact = directory / f"{host_id}-{pending.queue_id}-95000-105000"
    artifact.mkdir(parents=True)
    media_files = (artifact / "camera-a.mp4", artifact / "camera-a.jpg")
    media_files[0].write_bytes(b"durable local synthetic evidence clip")
    media_files[1].write_bytes(b"durable local synthetic keyframe")
    station.record_evidence_slice(
        pending.queue_id,
        at=model.HostInstant(110.0),
        media_results=json.dumps(
            [
                {
                    "media_path": "camera-a",
                    "clip_file": media_files[0].name,
                    "keyframe_file": media_files[1].name,
                    "actual_from": 95.0,
                    "actual_to": 105.0,
                    "material_generation": "original",
                    "sha256": hashlib.sha256(media_files[0].read_bytes()).hexdigest(),
                    "size_bytes": media_files[0].stat().st_size,
                }
            ]
        ),
        covered_from=95.0,
        covered_to=105.0,
    )
    references = tuple(f"{artifact.name}/{path.name}" for path in media_files)
    bytes_before = tuple(path.read_bytes() for path in media_files)
    path = "/api/v1/evidence/registrations"

    def central_count() -> int:
        with engine.connect() as connection:
            return int(
                connection.scalar(
                    text("SELECT count(*) FROM evidence_evidence WHERE evidence_id = ANY(:ids)"),
                    {"ids": list(references)},
                )
                or 0
            )

    try:
        assert central_count() == 0
        with client_for(engine, settings_for(engine, storage_root=dataset_storage_root)) as client:

            class SignedCenter:
                def __init__(self) -> None:
                    self.posted: list[str] = []
                    self.statuses: list[int] = []
                    self.drop_first_ack = True
                    self.conflict_digest = False

                def post(self, route: str, body: dict[str, object]) -> object:
                    assert route == path
                    sent_body = dict(body)
                    if self.conflict_digest:
                        sent_body["sha256"] = "b" * 64
                    response = client.post(
                        path,
                        json=sent_body,
                        headers=_host_headers(
                            runtime_topology, method="POST", path=path, body=sent_body
                        ),
                    )
                    self.statuses.append(response.status_code)
                    self.posted.append(str(body["evidence_id"]))
                    if self.drop_first_ack:
                        self.drop_first_ack = False
                        assert response.status_code == 200
                        raise center_api.CenterUnreachableError(
                            "center registered evidence but acknowledgement was lost"
                        )
                    return center_api.CenterResponse(response.status_code, response.content)

            center = SignedCenter()

            def drain() -> int:
                return cast(
                    int,
                    evidence_reporting.EvidenceReferenceReconciler(
                        state=state,
                        client=center,
                        host_id=host_id,
                        evidence_directory=directory,
                        interval=1.0,
                        clock=lambda: 111.0,
                    ).drain(limit=5),
                )

            assert drain() == 0
            assert central_count() == 1
            assert {row.name: row.depth for row in state.queue_status()}["evidence"] == 1
            (failed,) = station.evidence_ready_for_registration()
            assert "CenterUnreachableError" in (failed.last_error or "")
            state.close()
            state = local.open_local_state(
                database,
                evidence_source=lambda _: local.EvidenceSource(
                    wall_offset=0.0, media_paths=("camera-a",)
                ),
            )
            station = state.station(station_id)
            assert {row.name: row.depth for row in state.queue_status()}["evidence"] == 1
            assert len(station.evidence_ready_for_registration()) == 1

            center.conflict_digest = True
            assert drain() == 0
            (rejected,) = station.evidence_ready_for_registration()
            assert "HTTP 409" in (rejected.last_error or "")
            assert central_count() == 1

            center.conflict_digest = False
            assert drain() == 1
            assert center.statuses == [200, 409, 200, 200]
            assert central_count() == 2
            assert drain() == 0
            assert station.evidence_ready_for_registration() == ()
            assert {row.name: row.depth for row in state.queue_status()}["evidence"] == 0
            assert len(station.pending_evidence()) == 1  # 引用登记不代表中心接收媒体副本
            assert tuple(path.read_bytes() for path in media_files) == bytes_before
            with DatabaseSession(engine) as session:
                for reference, path_to_file in zip(references, media_files, strict=True):
                    saved = PostgresEvidenceRepository(session).find(reference)
                    assert saved is not None
                    assert saved.registration.reference == reference
                    assert (
                        saved.registration.sha256
                        == hashlib.sha256(path_to_file.read_bytes()).hexdigest()
                    )
    finally:
        state.close()
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM evidence_evidence WHERE evidence_id = ANY(:ids)"),
                {"ids": list(references)},
            )
