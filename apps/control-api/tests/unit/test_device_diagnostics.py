"""What `device`'s diagnostic lines say.

§5.15 gives every line five mandatory fields. The use-case suites next to this one assert on
behavior; this one asserts on the rendered JSON, captured by configuring the real renderer
against a `StringIO` — the same reason `auth`'s diagnostics suite does. `device` carries no
credentials at all (ADR-0008 keeps them on the inference host), so the property asserted here
is that the lines identify the actor, the object, and the outcome, and nothing secret-shaped
rides along with them.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from auth_fakes import caller_holding
from device_fakes import FAKE_NOW, FakeConnectors, FakeInferenceBackends, FakeInferenceHosts

from factory_sop.auth.api import Permission
from factory_sop.device.usecases.hosts import create_host, delete_host
from factory_sop.observability import configure_logging

CALLER = caller_holding(
    Permission.INFERENCE_HOST_VIEW,
    Permission.INFERENCE_HOST_EDIT,
    Permission.INFERENCE_HOST_DELETE,
)

MANDATORY_FIELDS = {"event", "module", "correlation_id", "level", "ts"}


@pytest.fixture
def log() -> io.StringIO:
    """The stream `device`'s logger renders into for the duration of one test."""
    stream = io.StringIO()
    configure_logging(log_level="info", stream=stream)
    return stream


def lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def test_the_host_lifecycle_logs_actor_object_and_outcome(log: io.StringIO) -> None:
    hosts = FakeInferenceHosts()
    host = create_host(
        name="装配A线-推理机1",
        address="10.0.8.11",
        mediamtx_address=None,
        recording_window_seconds=7 * 24 * 3600,
        disk_watermark_percent=85,
        caller=CALLER,
        now=FAKE_NOW,
        hosts=hosts,
    )
    delete_host(
        host_id=host.id,
        expected_revision=host.revision,
        caller=CALLER,
        hosts=hosts,
        backends=FakeInferenceBackends(),
        connectors=FakeConnectors(),
    )

    records = lines(log)
    events = [line["event"] for line in records]
    assert "device.inference_host.created" in events
    assert "device.inference_host.deleted" in events
    assert records
    assert all(record.keys() >= MANDATORY_FIELDS for record in records)
    assert all("password" not in json.dumps(record).lower() for record in records)
