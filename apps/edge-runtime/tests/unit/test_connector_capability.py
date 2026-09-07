"""What a connector's measured capability lets its point be used for.

§5.8 fixes the rule these assertions pin: judgment decides by the capability declaration,
never by the adapter's type. So the fitness question is answered here, over data, and a
second adapter changes no branch in it.

The other half is §5.21's conservatism: an unverified declaration is treated as insufficient
for every role rather than optimistically allowed. That is what lets an adapter be written
and tested before the device it talks to exists.
"""

from __future__ import annotations

import unittest

from harness import measured_capability as measured
from nvsop_contracts import (
    EdgePreservation,
    PointRole,
    Polled,
    Sequencing,
    TimestampSource,
    Unfitness,
    Unverified,
    unfit_for,
)

BUDGET = 0.2
"""How much of the 500 ms budget this role's point may spend on delivery (§5.6).

Passed in rather than named in the rule: which of the two start points applies is the binding
caller's to resolve, and a time literal on that path is what §5.19 forbids.
"""


class AnUnverifiedDeclarationCarriesNoRoleTest(unittest.TestCase):
    """§5.21 refuses conservatively: configuration saves offline, binding does not pass."""

    def test_every_role_is_refused_for_want_of_measurement(self) -> None:
        for role in PointRole:
            with self.subTest(role=role):
                self.assertEqual(
                    (Unfitness.CAPABILITY_UNVERIFIED,),
                    unfit_for(Unverified(), role=role, budget=BUDGET),
                    "an unverified point must not be bound to a role and then fail in the "
                    "field as indeterminate; it fails at binding time (§5.8)",
                )

    def test_nothing_else_is_claimed_about_it(self) -> None:
        self.assertEqual(
            (Unfitness.CAPABILITY_UNVERIFIED,),
            unfit_for(Unverified(), role=PointRole.START_SIGNAL, budget=0.0),
            "there are no measured values to find a second fault in, and inventing one "
            "would report a defect the device has not been shown to have",
        )


class APointThatCanLoseAnEdgeIsEvidenceOfNothingTest(unittest.TestCase):
    """A lost input edge cannot support a boundary or step observation."""

    def test_no_input_role_accepts_it(self) -> None:
        capability = measured(edges=EdgePreservation.MAY_DROP)

        for role in (
            PointRole.START_SIGNAL,
            PointRole.END_SIGNAL,
            PointRole.ORDERED_STEP,
            PointRole.UNORDERED_STEP,
        ):
            with self.subTest(role=role):
                self.assertEqual(
                    (Unfitness.MAY_DROP_EDGES,),
                    unfit_for(capability, role=role, budget=BUDGET),
                )


class OrderJudgmentNeedsAPointThatKeepsOrderTest(unittest.TestCase):
    """§5.8: 不保序的点位不可作为顺序判断依据."""

    def test_an_ordered_step_refuses_an_unsequenced_point(self) -> None:
        capability = measured(sequencing=Sequencing.UNSEQUENCED)

        self.assertEqual(
            (Unfitness.NOT_SEQUENCED,),
            unfit_for(capability, role=PointRole.ORDERED_STEP, budget=BUDGET),
        )

    def test_the_roles_that_do_not_read_order_accept_it(self) -> None:
        capability = measured(sequencing=Sequencing.UNSEQUENCED)

        for role in (PointRole.START_SIGNAL, PointRole.END_SIGNAL, PointRole.UNORDERED_STEP):
            with self.subTest(role=role):
                self.assertEqual(
                    (),
                    unfit_for(capability, role=role, budget=BUDGET),
                    "a boundary signal is one event, and an unordered template asks nothing "
                    "about sequence; refusing here would reject a point that works",
                )


class DeliveryDelayIsSpentAgainstTheResultsBudgetTest(unittest.TestCase):
    """§5.8: 最大投递延迟与该点位承担的结果类型的 500 ms 预算相抵 (§5.6)."""

    def test_a_delay_inside_the_budget_is_fit(self) -> None:
        self.assertEqual(
            (),
            unfit_for(
                measured(max_delivery_delay=BUDGET), role=PointRole.END_SIGNAL, budget=BUDGET
            ),
            "exactly the budget is within it: the point is allowed to spend what it was given",
        )

    def test_a_delay_past_the_budget_is_refused(self) -> None:
        capability = measured(max_delivery_delay=BUDGET + 0.01)

        self.assertEqual(
            (Unfitness.DELIVERY_TOO_SLOW,),
            unfit_for(capability, role=PointRole.END_SIGNAL, budget=BUDGET),
        )

    def test_a_polling_period_is_the_floor_on_the_delay_it_declares(self) -> None:
        with self.assertRaises(ValueError):
            measured(delivery=Polled(interval=1.0), max_delivery_delay=0.5)

    def test_a_polled_point_is_judged_on_the_same_declared_delay(self) -> None:
        capability = measured(delivery=Polled(interval=1.0), max_delivery_delay=1.1)

        self.assertEqual(
            (Unfitness.DELIVERY_TOO_SLOW,),
            unfit_for(capability, role=PointRole.UNORDERED_STEP, budget=BUDGET),
            "polling is not refused for being polling — it is refused when its period puts "
            "the delay past the budget, which is the same arithmetic a push point gets",
        )


class EveryFaultIsReportedAtOnceTest(unittest.TestCase):
    """The operator配置界面 has to show why, not the first reason of several (§5.8)."""

    def test_a_point_failing_three_ways_says_so(self) -> None:
        capability = measured(
            sequencing=Sequencing.UNSEQUENCED,
            edges=EdgePreservation.MAY_DROP,
            max_delivery_delay=BUDGET + 1.0,
        )

        self.assertEqual(
            (Unfitness.MAY_DROP_EDGES, Unfitness.NOT_SEQUENCED, Unfitness.DELIVERY_TOO_SLOW),
            unfit_for(capability, role=PointRole.ORDERED_STEP, budget=BUDGET),
            "declaration order, so the whole tuple is comparable as one object",
        )


class ADeviceClockIsAnAlignmentQuestionNotAFitnessOneTest(unittest.TestCase):
    """§5.8: 时间戳来源决定时间锚定误差如何计入 `IO_TIME_UNALIGNED` 阈值.

    It does not decide the role. A device-clock point is bindable; whether one particular
    signal off it could be placed on the video timeline is the adapter's per-reading answer,
    and it arrives at judgment as `TimeAlignment`.
    """

    def test_a_device_clock_point_is_fit_for_every_role(self) -> None:
        capability = measured(timestamps=TimestampSource.DEVICE_CLOCK)

        for role in PointRole:
            with self.subTest(role=role):
                self.assertEqual((), unfit_for(capability, role=role, budget=BUDGET))


if __name__ == "__main__":
    unittest.main()
