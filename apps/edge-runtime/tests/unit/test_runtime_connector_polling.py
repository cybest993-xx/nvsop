from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nvsop_contracts import (
    EdgePreservation,
    Measured,
    Polled,
    Sequencing,
    TimestampSource,
)

from edge_runtime.connectors.port import (
    ConnectorHealth,
    InputPoint,
    PointState,
    Reachability,
    Reading,
    ReadResult,
    Written,
)
from edge_runtime.connectors.runtime import ConnectorRuntime
from edge_runtime.judgment.evidence import EvidenceMargins
from edge_runtime.judgment.model import HostInstant, Ordering, RuntimeParameters, Template
from edge_runtime.local_state.store import open_local_state
from edge_runtime.runtime import AutonomousStation
from edge_runtime.supervisor.inputs import TimeAlignment
from edge_runtime.supervisor.startup import resume_station


class PollingConnector:
    capability = Measured(
        delivery=Polled(interval=0.1),
        max_delivery_delay=0.1,
        sequencing=Sequencing.SEQUENCED,
        edges=EdgePreservation.PRESERVED,
        timestamps=TimestampSource.HOST_RECEIPT,
    )

    def __init__(self) -> None:
        self.read_count = 0

    def read(self, point: InputPoint, /, *, timeout: float) -> ReadResult:
        del point, timeout
        self.read_count += 1
        return Reading(
            state=PointState.ACTIVE,
            at=HostInstant(1.0),
            alignment=TimeAlignment.ALIGNED,
        )

    def write(self, point: object, state: object, /, *, timeout: float) -> Written:
        del point, state, timeout
        return Written(at=HostInstant(1.0))

    def probe(self, /, *, timeout: float) -> ConnectorHealth:
        del timeout
        return ConnectorHealth(reachability=Reachability.REACHABLE)


class EndedInput:
    ended = True

    def __init__(self) -> None:
        self.closed = False

    def next_input(self, *, timeout: float | None) -> None:
        del timeout
        return None

    def close(self) -> None:
        self.closed = True


class RuntimeConnectorPollingTest(unittest.TestCase):
    def test_station_loop_polls_a_composed_connector_runtime(self) -> None:
        cadence = ConnectorRuntime(
            connector_id="connector-cadence",
            connector=PollingConnector(),
            input_points=(InputPoint(label="start", address="DI-01"),),
            timeout=1.0,
        )
        self.assertEqual(10.0, cadence.next_due(now=10.0))
        self.assertEqual(10.0, cadence.next_due(now=10.05))
        self.assertEqual(1, len(cadence.poll(now=10.0)))
        self.assertEqual(10.1, cadence.next_due(now=10.05))

        connector = PollingConnector()
        runtime = ConnectorRuntime(
            connector_id="connector-a",
            connector=connector,
            input_points=(InputPoint(label="start", address="DI-01"),),
            timeout=1.0,
        )
        with tempfile.TemporaryDirectory() as temporary:
            state = open_local_state(str(Path(temporary) / "state.sqlite"))
            supervisor = resume_station(
                state.station("station-a"),
                template=Template(
                    steps=("start",),
                    ordering=Ordering.ORDERED,
                    start_signal="start",
                ),
                parameters=RuntimeParameters(idle_timeout=10.0, step_deadline=10.0),
                margins=EvidenceMargins(leading=0.0, trailing=0.0),
            )
            source = EndedInput()
            station = AutonomousStation(
                supervisor=supervisor,
                source=source,
                station_id="station-a",
                connector_runtimes=(runtime,),
            )

            station.run_forever(should_stop=lambda: False)

            self.assertEqual(1, connector.read_count)
            self.assertTrue(source.closed)
            state.close()


if __name__ == "__main__":
    unittest.main()
