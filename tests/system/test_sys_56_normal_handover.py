"""S056：真实 Center/PostgreSQL、双 SQLite 配置确认和 ISAPI 写入闭环。"""

from __future__ import annotations

import threading
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from uuid import UUID

from conftest import BootstrapCommand, csrf_header, log_in
from edge_runtime.connectors.hikvision import CANDIDATE_PROFILE, IsapiConnector
from edge_runtime.connectors.port import OutputPoint, PointState, Refused, WriteRefusal, Written
from edge_runtime.connectors.transport import UrllibIsapiTransport
from edge_runtime.connectors.writes import OutputDispatcher, WriteRequest
from edge_runtime.judgment.model import HostInstant
from edge_runtime.local_state.store import LocalState, open_local_state
from edge_runtime.runtime import SQLiteWriteLedger, StationOutputWriteGate, StationWriteLifecycle
from httpx2 import Client
from sqlalchemy import Engine
from test_sys_92_point_management import MEASURED_CAPABILITY
from test_sys_93_delegated_connection_test_over_real_center_http import (
    _configure_confirmed_station,
    _create_target,
    _edge_headers,
)

from factory_sop.execution.adapters.repository import PostgresExecutionGrantRepository
from factory_sop.execution.usecases import RepositoryExecutionLeaseGateway
from factory_sop.identifiers import new_id
from factory_sop.persistence import session_factory
from nvsop_contracts import ConfigurationBundle, ExecutionLease, configuration_from_wire


class OutputDevice(ThreadingHTTPServer):
    puts: int = 0


class OutputHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()

    def do_PUT(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        cast(OutputDevice, self.server).puts += 1
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


def test_new_host_writes_only_after_grant_and_durable_configuration_confirmation(
    client: Client,
    bootstrap: BootstrapCommand,
    engine: Engine,
    tmp_path: Path,
) -> None:
    log_in(client, bootstrap)
    csrf = csrf_header(client)
    new = _create_target(client, csrf, "8")
    old = _create_target(client, csrf, "9")
    _configure_confirmed_station(client, csrf, new, "http://127.0.0.1:8000")
    measured = client.put(
        f"/api/v1/connectors/{new['connector_id']}/capability",
        headers={**csrf, "If-Match": "1"},
        json=MEASURED_CAPABILITY,
    )
    assert measured.status_code == 200, measured.text
    point = client.post(
        "/api/v1/points",
        headers=csrf,
        json={
            "station_id": new["station_id"],
            "connector_id": new["connector_id"],
            "direction": "output",
            "identifier": "2",
            "semantic_label": "停线联锁",
        },
    )
    assert point.status_code == 201, point.text
    # 初始事实：目标拓扑已经部署，旧机仍持有其历史配置与执行权；新机尚无写权。
    with session_factory(engine).begin() as session:
        initial_grant = RepositoryExecutionLeaseGateway(
            PostgresExecutionGrantRepository(session)
        ).acquire(
            station_id=UUID(new["station_id"]),
            holder_host_id=UUID(old["host_id"]),
            request_id=new_id(),
            now=datetime.now(UTC),
        )

    def pull(target: dict[str, str]) -> ConfigurationBundle:
        path = f"/api/v1/inference-hosts/{target['host_id']}/configuration"
        response = edge.get(
            path,
            headers=_edge_headers(
                target["host_id"],
                target["host_private_key"],
                method="GET",
                path=path,
            ),
        )
        assert response.status_code == 200, response.text
        return configuration_from_wire(response.json())

    with (
        Client(base_url=client.base_url, verify=False, trust_env=False) as edge,
        closing(open_local_state(str(tmp_path / "old.sqlite"))) as old_state,
        closing(open_local_state(str(tmp_path / "new.sqlite"))) as new_state,
    ):
        before = pull(new)
        assert before.execution_grants == ()
        old_state.configuration().confirm(
            replace(
                before,
                host_id=old["host_id"],
                execution_grants=(
                    ExecutionLease(
                        station_id=new["station_id"],
                        grant_id=str(initial_grant.grant_id),
                        holder_host_id=old["host_id"],
                        lease_expires_at=initial_grant.lease_expires_at.isoformat(),
                    ),
                ),
            ),
            confirmed_at=1.0,
        )
        new_state.configuration().confirm(before, confirmed_at=1.0)
        created = client.post(
            "/api/v1/execution/normal-handovers",
            headers={
                **csrf,
                "Idempotency-Key": "sys56-handover",
            },
            json={
                "station_id": new["station_id"],
                "from_host_id": old["host_id"],
                "to_host_id": new["host_id"],
            },
        )
        assert created.status_code == 201, created.text
        record = created.json()
        claim_path = "/api/v1/device-commands/execution-handover/next"
        claim = edge.get(
            claim_path,
            headers=_edge_headers(
                old["host_id"],
                old["host_private_key"],
                method="GET",
                path=claim_path,
            ),
        )
        assert claim.status_code == 200, claim.text
        old_state.execution_leases().stop_writes(
            new["station_id"],
            grant_id=record["grant_id"],
            stopped_at=2.0,
        )
        path = f"/api/v1/device-commands/{record['command_id']}/execution-handover-result"
        body = {
            "handover_id": record["handover_id"],
            "grant_id": record["grant_id"],
            "outcome": "confirmed",
            "failure_code": None,
            "detail": None,
        }
        confirmed = edge.post(
            path,
            json=body,
            headers=_edge_headers(
                old["host_id"],
                old["host_private_key"],
                method="POST",
                path=path,
                body=body,
                claim_token=claim.json()["claim_token"],
            ),
        )
        assert confirmed.status_code == 200, confirmed.text
        granted = client.post(
            f"/api/v1/execution/normal-handovers/{record['handover_id']}/grant",
            headers=csrf,
        )
        assert granted.status_code == 200, granted.text
        candidate = pull(new)
        assert candidate.execution_grants[0].grant_id == granted.json()["new_grant_id"]
        assert pull(old).execution_grants == ()

        device = OutputDevice(("127.0.0.1", 0), OutputHandler)
        thread = threading.Thread(target=device.serve_forever, daemon=True)
        thread.start()
        try:
            output = OutputPoint(label="停线联锁", address="2")

            def dispatcher(state: LocalState) -> OutputDispatcher:
                return OutputDispatcher(
                    connector=IsapiConnector(
                        transport=UrllibIsapiTransport(
                            base_url=f"http://127.0.0.1:{device.server_port}",
                            username="synthetic",
                            password="synthetic",  # pragma: allowlist secret
                        ),
                        profile=CANDIDATE_PROFILE,
                        capability=candidate.stations[0].connectors[0].capability,
                    ),
                    ledger=SQLiteWriteLedger(state.disposal()),
                    gate=StationOutputWriteGate(
                        station_id=new["station_id"],
                        connector_id=new["connector_id"],
                        output_points=(output,),
                        leases=state.execution_leases(),
                        lifecycle=StationWriteLifecycle(),
                        clock=lambda: datetime.now(UTC).timestamp(),
                    ),
                    diagnostics=lambda event: None,
                )

            request = WriteRequest(
                point=output,
                state=PointState.ACTIVE,
                key="sys56-output",
                actor="supervisor",
                timeout=1.0,
                capability_budget=1.0,
                station_id=new["station_id"],
                connector_id=new["connector_id"],
                attempt_at=HostInstant(3.0),
                lease_seconds=5.0,
            )
            new_output = dispatcher(new_state)
            denied = new_output.write(request)
            assert isinstance(denied, Refused)
            assert denied.reason is WriteRefusal.EXECUTION_LEASE_MISSING
            new_state.configuration().confirm(candidate, confirmed_at=3.0)
            assert isinstance(new_output.write(request), Written)
            denied_old = dispatcher(old_state).write(request)
            assert isinstance(denied_old, Refused)
            assert denied_old.reason is WriteRefusal.WRITE_STOPPED
            assert device.puts == 1
        finally:
            device.shutdown()
            thread.join(timeout=5)
            device.server_close()
