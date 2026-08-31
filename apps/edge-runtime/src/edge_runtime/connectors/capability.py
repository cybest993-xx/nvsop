"""能力声明: what a connector measurably does, and which judgment roles that permits.

§5.8 fixes two things this module implements. First, **judgment decides by the capability
declaration, not by the adapter's type** — so the fitness rule is a function over data, and
adding a second adapter changes no branch in it. Second, **the declaration is a measured
value, not the adapter author's promise** — so "not measured yet" is a state of its own, and
§5.21 requires it be read as insufficient for every role rather than optimistically allowed.

Where the check runs: at **template binding**, not in the judgment path (§5.8). A point whose
declaration cannot carry the role it was given fails the binding with a stated reason; it must
never be accepted and then show up in the field as an indeterminate verdict.

The center owns binding validation and must call this same rule rather than restate it — a
second copy would let the two sides disagree about what a point may do, which is the drift
§5.8 warns about. It lives here because `edge-runtime` is its only caller today and the
declaration travels on the pull contract; it moves to `packages/contracts` when the center
lands as the second real caller (harness §2).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True, slots=True)
class Pushed:
    """The device announces a change. Nothing rides along: there is no period to declare."""


@dataclass(frozen=True, slots=True)
class Polled:
    """We ask, on a period. That period is the floor on how stale an answer can be.

    §5.8: 轮询时下条一并给出周期. It is carried on the delivery mode rather than as an
    optional field beside it, so a push declaration cannot hold a meaningless period and a
    polled one cannot omit its own.
    """

    interval: float

    def __post_init__(self) -> None:
        if self.interval <= 0:
            raise ValueError(f"a polling period must be positive, got {self.interval}")


Delivery = Pushed | Polled
"""投递方式. §5.8's first declaration, and the only one that carries a value of its own."""


class Sequencing(Enum):
    """是否保序: whether repeated changes on one point arrive in the order they happened."""

    SEQUENCED = "sequenced"
    UNSEQUENCED = "unsequenced"


class EdgePreservation(Enum):
    """是否可能丢边沿: whether a change between two samples can vanish unseen.

    A device that latches its alarm state until read preserves edges even when polled; one
    whose status endpoint answers with the instantaneous level does not. Which it is, is
    measured — polling does not imply dropping, and assuming it would reject a device that
    works (§5.21).
    """

    PRESERVED = "preserved"
    MAY_DROP = "may_drop"


class TimestampSource(Enum):
    """时间戳来源. Decides how anchoring error counts toward `IO_TIME_UNALIGNED` (§5.8).

    It does not decide the role: a device-clock point is bindable, and whether one particular
    reading could be placed on the video timeline is the adapter's per-reading answer.
    """

    DEVICE_CLOCK = "device_clock"
    HOST_RECEIPT = "host_receipt"


@dataclass(frozen=True, slots=True)
class Measured:
    """All five declarations, measured against a real device (§5.8)."""

    delivery: Delivery
    max_delivery_delay: float
    """最大投递延迟: the upper bound from the physical change to the observation being
    available. Spent against the 500 ms budget of the result class this point serves (§5.6)."""

    sequencing: Sequencing
    edges: EdgePreservation
    timestamps: TimestampSource

    def __post_init__(self) -> None:
        if self.max_delivery_delay < 0:
            raise ValueError(f"a delivery delay cannot be negative, got {self.max_delivery_delay}")
        if isinstance(self.delivery, Polled) and self.max_delivery_delay < self.delivery.interval:
            raise ValueError(
                f"a polled point cannot deliver faster than its period: declared "
                f"{self.max_delivery_delay}s against a {self.delivery.interval}s period. "
                "§5.8: 轮询周期即最大投递延迟的主要成分。"
            )


@dataclass(frozen=True, slots=True)
class Unverified:
    """Configuration was saved without a device to measure against.

    Same semantics as offline device configuration (control-plane.md §5.3, Q31): the
    configuration persists and displays as unverified. It carries no measured values —
    holding some would invite reading them, and §5.8 says a declaration is a measurement
    rather than a promise.
    """


Capability = Measured | Unverified
"""One point's declaration. `Unverified` is a state, not a missing value: `unfit_for` reads it
as insufficient for every role (§5.21)."""


class PointRole(Enum):
    """What a template asked this point to be.

    Four rather than two, because the two step roles are refused for different reasons: an
    ordered template reads sequence off its steps and an unordered one does not.
    """

    START_SIGNAL = "start_signal"
    END_SIGNAL = "end_signal"
    ORDERED_STEP = "ordered_step"
    UNORDERED_STEP = "unordered_step"


class Unfitness(Enum):
    """Why a point cannot carry a role. Reported at binding, shown to the operator (§5.8)."""

    CAPABILITY_UNVERIFIED = "capability_unverified"
    MAY_DROP_EDGES = "may_drop_edges"
    NOT_SEQUENCED = "not_sequenced"
    DELIVERY_TOO_SLOW = "delivery_too_slow"


def unfit_for(capability: Capability, *, role: PointRole, budget: float) -> tuple[Unfitness, ...]:
    """Every reason this point cannot carry this role; empty when it can.

    `budget` is how much of the 500 ms this role's result class allows to be spent on
    delivery (§5.6). Passed in with no default: which of the two start points applies is the
    binding caller's to resolve, and a time literal on this path is what §5.19 forbids.

    All faults are returned rather than the first, because the configuration page has to
    tell the operator what to fix. Declaration order, so the tuple compares as one object.
    """
    if isinstance(capability, Unverified):
        # The only finding available. There are no measured values to fault, and naming a
        # second one would report a defect the device has not been shown to have.
        return (Unfitness.CAPABILITY_UNVERIFIED,)

    found: list[Unfitness] = []
    if capability.edges is EdgePreservation.MAY_DROP:
        # §5.8 says this for the boundary signals. It holds for a step for a sharper reason:
        # a dropped step edge is reported as `MISSED_STEP`, and a false failing verdict is
        # what §5.2 forbids outright — the "没做 vs 没看到" confusion measured in §2.1.
        found.append(Unfitness.MAY_DROP_EDGES)
    if role is PointRole.ORDERED_STEP and capability.sequencing is Sequencing.UNSEQUENCED:
        found.append(Unfitness.NOT_SEQUENCED)
    if capability.max_delivery_delay > budget:
        found.append(Unfitness.DELIVERY_TOO_SLOW)
    return tuple(found)
