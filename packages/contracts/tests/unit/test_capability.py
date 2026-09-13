"""共享能力规则在跨进程接口处的行为。"""

from __future__ import annotations

import unittest

from nvsop_contracts import (
    EdgePreservation,
    Measured,
    PointRole,
    Polled,
    Pushed,
    Sequencing,
    TimestampSource,
    Unfitness,
    Unverified,
    capability_from_wire,
    capability_to_wire,
    unfit_for,
)


class SafetyOutputCapabilityTest(unittest.TestCase):
    def test_output_reads_verification_and_latency_not_input_edge_traits(self) -> None:
        capability = Measured(
            delivery=Polled(interval=0.05),
            max_delivery_delay=0.1,
            sequencing=Sequencing.UNSEQUENCED,
            edges=EdgePreservation.MAY_DROP,
            timestamps=TimestampSource.DEVICE_CLOCK,
        )

        self.assertEqual(
            (),
            unfit_for(capability, role=PointRole.SAFETY_OUTPUT, budget=0.1),
        )
        self.assertEqual(
            (Unfitness.DELIVERY_TOO_SLOW,),
            unfit_for(capability, role=PointRole.SAFETY_OUTPUT, budget=0.09),
        )
        self.assertEqual(
            (Unfitness.CAPABILITY_UNVERIFIED,),
            unfit_for(Unverified(), role=PointRole.SAFETY_OUTPUT, budget=0.1),
        )

    def test_a_negative_budget_is_not_a_role_budget(self) -> None:
        with self.assertRaises(ValueError):
            unfit_for(Unverified(), role=PointRole.SAFETY_OUTPUT, budget=-0.01)


class CapabilityWireRoundTripTest(unittest.TestCase):
    def test_unverified_round_trips_without_inventing_measured_fields(self) -> None:
        document = capability_to_wire(Unverified())

        self.assertEqual({"verification": "unverified"}, document)
        self.assertEqual(Unverified(), capability_from_wire(document))

    def test_pushed_measurement_round_trips_as_one_known_document(self) -> None:
        capability = Measured(
            delivery=Pushed(),
            max_delivery_delay=0.08,
            sequencing=Sequencing.SEQUENCED,
            edges=EdgePreservation.PRESERVED,
            timestamps=TimestampSource.HOST_RECEIPT,
        )

        document = capability_to_wire(capability)

        self.assertEqual(
            {
                "verification": "measured",
                "delivery": "pushed",
                "polling_interval_seconds": None,
                "max_delivery_delay_seconds": 0.08,
                "sequencing": "sequenced",
                "edge_preservation": "preserved",
                "timestamp_source": "host_receipt",
            },
            document,
        )
        self.assertEqual(capability, capability_from_wire(document))

    def test_polled_measurement_round_trips_with_its_required_period(self) -> None:
        capability = Measured(
            delivery=Polled(interval=0.05),
            max_delivery_delay=0.12,
            sequencing=Sequencing.SEQUENCED,
            edges=EdgePreservation.PRESERVED,
            timestamps=TimestampSource.DEVICE_CLOCK,
        )

        self.assertEqual(capability, capability_from_wire(capability_to_wire(capability)))

    def test_unknown_or_partial_documents_are_not_silently_interpreted(self) -> None:
        bad_documents = (
            {"verification": "future"},
            {"verification": "unverified", "delivery": "pushed"},
            {
                "verification": "measured",
                "delivery": "polled",
                "polling_interval_seconds": None,
                "max_delivery_delay_seconds": 0.1,
                "sequencing": "sequenced",
                "edge_preservation": "preserved",
                "timestamp_source": "host_receipt",
            },
        )
        for document in bad_documents:
            with self.subTest(document=document), self.assertRaises(ValueError):
                capability_from_wire(document)


if __name__ == "__main__":
    unittest.main()
