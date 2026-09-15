"""The connector seam: what every adapter provides, and in what vocabulary.

One interface covering the four things §5.8 and the E6 ticket require of a connector —
reading input points, writing output points, reporting connection health, and declaring its
measured capability. The Hikvision ISAPI adapter is the first implementation; the board card
(P12) is the second, and it must add no branch above this seam.

Everything here is data or a `Protocol`. The rule that gives this file its shape is that an
adapter **reports facts and never decides**: it says a point could not be reached, or that a
reading could not be placed on the video timeline, and the conservative reading of those
facts happens above it (§5.2). An adapter that returned a guess would put the safety
invariant beyond reach of the judgment core, which is the one place §5.2 requires it to hold.

Standard library only (edge-autonomy.md §5.11).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from nvsop_contracts import Capability

from edge_runtime.judgment.model import HostInstant
from edge_runtime.supervisor.inputs import TimeAlignment


@dataclass(frozen=True, slots=True)
class InputPoint:
    """A readable point: a signal position with a direction and a semantic label (CONTEXT.md).

    Input and output are separate types rather than one type with a direction field, so a
    write cannot be aimed at an input point by passing the wrong value — the two are used at
    different seams and confusing them would drive a physical actuator by mistake.
    """

    label: str
    """语义标签, such as 工件到位. What the template references and what reaches judgment as a
    step signal; the core cannot tell it from an action number, which is §5.8's constraint."""

    address: str
    """The device's own identifier for it — an ISAPI input ID, a board card channel. Never
    referenced by a template, which references a point by semantic label instead (§5.8)."""


@dataclass(frozen=True, slots=True)
class OutputPoint:
    """A writable point, such as a 停线联锁 relay.

    Driving one is the highest-impact thing this system does, so every attempt produces a
    diagnostic event naming the operator and the target point (§5.15, Q37).
    """

    label: str
    address: str


class PointState(Enum):
    """A point's level. Two values: these are alarm contacts, not analog channels."""

    ACTIVE = "active"
    INACTIVE = "inactive"


@dataclass(frozen=True, slots=True)
class Reading:
    """One point's level at one instant, already on the host's monotonic clock.

    The adapter converts: it holds the device's timestamp and whatever offset it has, and its
    capability declaration says how well it can (§5.8). What crosses this seam is the clock
    judgment measures on, plus whether that conversion was trustworthy.
    """

    state: PointState
    at: HostInstant
    alignment: TimeAlignment
    """Whether this reading could be placed on the video timeline (§5.8).

    The adapter's answer, not the runtime's: a host-receipt timestamp is aligned by
    construction within the declared delivery delay, while a device-clock timestamp is
    aligned only while the offset is known and inside the tolerance.
    """


@dataclass(frozen=True, slots=True)
class Unreachable:
    """The point could not be read at all. `detail` is triage text, never a level.

    A separate result rather than a `Reading | None`, so a caller cannot read "no answer" as
    "no change" — that is the optimistic reading that turns a cut cable into a passing pass
    (§5.2). It reaches judgment as `IO_SIGNAL_LOST`.
    """

    detail: str


ReadResult = Reading | Unreachable
"""What reading one input point produced. Exhaustive: there is no third answer, and in
particular there is no "unchanged" — that is inferred above this seam by comparing."""


class PolledInput(Protocol):
    """An adapter that answers when asked.

    §5.8 leaves 投递方式 to measurement, and the Hikvision status endpoint is a GET, so this
    is the shape the first adapter has. A pushing adapter declares its own protocol when one
    is measured to exist — writing that protocol now would be scaffolding for an endpoint
    whose existence is still a 待实测 item (§5.8).

    `timeout` is required on the read as it is on the write: a poll that blocks past its own
    period stops being a poll, and a default here would put a time literal on the path §5.19
    keeps clear of them.
    """

    def read(self, point: InputPoint, /, *, timeout: float) -> ReadResult: ...


class WriteRefusal(Enum):
    """Why a write was not attempted. Structured, so the caller can act on it (§5.8).

    Refusal precedes the attempt, which is what separates it from a failure: nothing was
    sent to the device, so nothing physical may have happened.
    """

    CAPABILITY_UNVERIFIED = "capability_unverified"
    """§5.21: 未验证能力…保守拒绝. Driving an interlock through a connector nobody has
    measured is the one place optimism is least affordable."""

    DELIVERY_TOO_SLOW = "delivery_too_slow"
    """连接器的实测最大延迟超出安全输出角色的调用方预算。"""

    POINT_UNREACHABLE = "point_unreachable"
    """设备访问尚未建立, 本次写入没有发送到目标点位。"""

    OUTPUT_NOT_CONFIGURED = "output_not_configured"
    """当前工位的已确认输出拓扑不包含请求目标。"""

    LEASE_ACTIVE = "lease_active"
    """同一幂等键已有活动物理尝试, 本次写入没有发送。"""


@dataclass(frozen=True, slots=True)
class Written:
    """设备已接受写入。"""

    at: HostInstant


@dataclass(frozen=True, slots=True)
class Refused:
    """写入尚未尝试, 并返回明确的结构化原因。"""

    reason: WriteRefusal
    detail: str = ""


@dataclass(frozen=True, slots=True)
class TimedOut:
    """设备在超时时间内没有应答, 物理结果可能已经发生。"""

    after: float


@dataclass(frozen=True, slots=True)
class Unknown:
    """账本无法确认物理结果的终态结果; 同一幂等键不能自动重放。"""

    detail: str


@dataclass(frozen=True, slots=True)
class Failed:
    """设备已应答, 但返回了拒绝或错误。"""

    detail: str


WriteOutcome = Written | Refused | TimedOut | Unknown | Failed
"""一次写入尝试的结构化结果, 可由 local_disposal 持久化。"""


class OutputWriter(Protocol):
    """An adapter that can drive an output point.

    `timeout` is required rather than defaulted: an idempotency key, a timeout and a
    structured failure result are the ticket's three requirements for a write, and a default
    here would put a time literal on the physical-control path (§5.19).
    """

    def write(
        self, point: OutputPoint, state: PointState, /, *, timeout: float
    ) -> WriteOutcome: ...


class Reachability(Enum):
    """`device_connector.健康状态`: 设备级可达性, written by the inference host (§5.7).

    Not 观测有效性 — that is `monitor_stream_health` and the impairments this package
    reports. This one serves the operations view: can the center's device page say this
    connector is answering. 未验证 is a real third value, not an absence: 测试连接 executes a
    real request and 不返回模拟成功 (control-plane.md §5.3).
    """

    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class ConnectorHealth:
    """The last connection result, as the center's device page shows it."""

    reachability: Reachability
    detail: str = ""


class HealthProbe(Protocol):
    """An adapter that can be asked whether its device is answering."""

    def probe(self, /, *, timeout: float) -> ConnectorHealth: ...


class Connector(PolledInput, OutputWriter, HealthProbe, Protocol):
    """The whole seam: input, output, health, and the capability that bounds all three.

    Composed from the three narrower protocols rather than declared flat, because a caller
    that only reads should depend only on `PolledInput`. `capability` is on the whole
    connector rather than per point: the five declarations in §5.8 are properties of how this
    adapter talks to this device, which is what makes them measurable at all.
    """

    @property
    def capability(self) -> Capability: ...
