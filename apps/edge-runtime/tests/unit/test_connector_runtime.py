from __future__ import annotations

import unittest

from nvsop_contracts import (
    EdgePreservation,
    Measured,
    Polled,
    Pushed,
    Sequencing,
    TimestampSource,
    Unverified,
)

from edge_runtime.connectors.port import ConnectorHealth, InputPoint, Reachability
from edge_runtime.connectors.runtime import ConnectorRuntime, ConnectorRuntimeSet


class FakeConnector:
    def __init__(self, capability: object) -> None:
        self.capability = capability
        self.reads = 0

    def read(self, point: InputPoint, /, *, timeout: float) -> object:
        self.reads += 1
        raise AssertionError("the test connector should not be polled")

    def write(self, point: object, state: object, /, *, timeout: float) -> object:
        raise AssertionError("not used")

    def probe(self, /, *, timeout: float) -> ConnectorHealth:
        return ConnectorHealth(Reachability.UNVERIFIED)


class ConnectorRuntimeTests(unittest.TestCase):
    def test_unverified_runtime_has_no_polling_side_effect(self) -> None:
        connector = FakeConnector(Unverified())
        runtime = ConnectorRuntime(
            connector_id="connector-a",
            connector=connector,  # type: ignore[arg-type]
            input_points=(InputPoint(label="start", address="1"),),
            timeout=1.0,
        )
        self.assertIsNone(runtime.polling_interval)
        self.assertIsNone(runtime.next_due(now=1.0))
        self.assertEqual(runtime.poll(), ())
        self.assertEqual(connector.reads, 0)

    def test_pushed_capability_is_rejected_at_construction(self) -> None:
        capability = Measured(
            delivery=Pushed(),
            max_delivery_delay=0.1,
            sequencing=Sequencing.SEQUENCED,
            edges=EdgePreservation.PRESERVED,
            timestamps=TimestampSource.HOST_RECEIPT,
        )
        with self.assertRaises(ValueError):
            ConnectorRuntime(
                connector_id="connector-a",
                connector=FakeConnector(capability),  # type: ignore[arg-type]
                input_points=(),
                timeout=1.0,
            )

    def test_runtime_set_has_one_runtime_per_configured_connector(self) -> None:
        capability = Measured(
            delivery=Polled(interval=0.5),
            max_delivery_delay=0.5,
            sequencing=Sequencing.SEQUENCED,
            edges=EdgePreservation.PRESERVED,
            timestamps=TimestampSource.HOST_RECEIPT,
        )
        values = {
            "connector-a": FakeConnector(capability),
            "connector-b": FakeConnector(Unverified()),
        }
        runtimes = ConnectorRuntimeSet(connectors=values, input_points={}, timeout=1.0)  # type: ignore[arg-type]
        self.assertEqual(len(runtimes.runtimes), 2)
        self.assertEqual(runtimes.runtime("connector-a").polling_interval, 0.5)
        self.assertIsNone(runtimes.runtime("connector-b").polling_interval)


if __name__ == "__main__":
    unittest.main()
