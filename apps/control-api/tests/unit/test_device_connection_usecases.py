"""The connection-test use case records real probe outcomes at its persistence seam."""

from __future__ import annotations

from datetime import UTC, datetime

from auth_fakes import caller_holding
from device_fakes import FakeInferenceBackends, FakeInferenceHosts, FakeProbe

from factory_sop.auth.api import Permission
from factory_sop.device.model import ConnectionState
from factory_sop.device.probing import ProbeReport
from factory_sop.device.usecases.connection import test_backend_connection as run_backend_connection

NOW = datetime(2026, 9, 7, 8, 0, tzinfo=UTC)
CALLER = caller_holding(Permission.INFERENCE_BACKEND_EDIT)


def test_a_successful_probe_records_the_models_and_moves_the_revision() -> None:
    hosts = FakeInferenceHosts()
    backends = FakeInferenceBackends()
    host = hosts.register(name="装配A线-推理机1")
    backend = backends.register(host_id=host.id, base_url="http://10.0.8.11:8000")
    probe = FakeProbe(
        report=ProbeReport(
            status=ConnectionState.SUCCESS,
            model_ids=("ds_sop_model", "qwen2-7b"),
        )
    )

    tested = run_backend_connection(
        backend_id=backend.id,
        expected_revision=backend.revision,
        caller=CALLER,
        now=NOW,
        probe=probe,
        backends=backends,
    )

    assert tested.connection_state is ConnectionState.SUCCESS
    assert tested.connection_checked_at == NOW
    assert tested.self_reported_model_ids == ("ds_sop_model", "qwen2-7b")
    assert tested.self_reported_at == NOW
    assert tested.revision == 2
    assert probe.asked_for == ["http://10.0.8.11:8000"]


def test_a_failed_probe_records_failure_and_clears_stale_model_identity() -> None:
    hosts = FakeInferenceHosts()
    backends = FakeInferenceBackends()
    host = hosts.register(name="装配A线-推理机1")
    backend = backends.register(
        host_id=host.id,
        base_url="http://10.0.8.11:8000",
        connection_state=ConnectionState.SUCCESS,
        connection_checked_at=NOW,
        self_reported_model_ids=("old-model",),
        self_reported_at=NOW,
    )
    probe = FakeProbe(
        report=ProbeReport(status=ConnectionState.FAILURE, detail="connection refused")
    )

    tested = run_backend_connection(
        backend_id=backend.id,
        expected_revision=backend.revision,
        caller=CALLER,
        now=NOW,
        probe=probe,
        backends=backends,
    )

    assert tested.connection_state is ConnectionState.FAILURE
    assert tested.connection_checked_at == NOW
    assert tested.connection_detail == "connection refused"
    assert tested.self_reported_model_ids == ()
    assert tested.self_reported_at is None
    assert tested.revision == 2
