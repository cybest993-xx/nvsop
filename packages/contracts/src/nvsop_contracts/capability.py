"""连接器能力声明，以及点位可承担角色的唯一适配规则。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import assert_never


@dataclass(frozen=True, slots=True)
class Pushed:
    """设备主动投递变化，不携带轮询周期。"""


@dataclass(frozen=True, slots=True)
class Polled:
    """运行时按周期读取，周期是观测陈旧程度的下界。"""

    interval: float

    def __post_init__(self) -> None:
        if self.interval <= 0:
            raise ValueError(f"a polling period must be positive, got {self.interval}")


Delivery = Pushed | Polled
"""投递方式；只有轮询方式携带周期。"""


class Sequencing(Enum):
    """同一点位的多次变化是否保证按发生顺序到达。"""

    SEQUENCED = "sequenced"
    UNSEQUENCED = "unsequenced"


class EdgePreservation(Enum):
    """两次采样之间的瞬时变化是否可能消失。"""

    PRESERVED = "preserved"
    MAY_DROP = "may_drop"


class TimestampSource(Enum):
    """外部信号时间戳的来源。"""

    DEVICE_CLOCK = "device_clock"
    HOST_RECEIPT = "host_receipt"


@dataclass(frozen=True, slots=True)
class Measured:
    """经真实设备测量得到的五项能力声明。"""

    delivery: Delivery
    max_delivery_delay: float
    sequencing: Sequencing
    edges: EdgePreservation
    timestamps: TimestampSource

    def __post_init__(self) -> None:
        if self.max_delivery_delay < 0:
            raise ValueError(f"a delivery delay cannot be negative, got {self.max_delivery_delay}")
        if isinstance(self.delivery, Polled) and self.max_delivery_delay < self.delivery.interval:
            raise ValueError(
                f"a polled point cannot deliver faster than its period: declared "
                f"{self.max_delivery_delay}s against a {self.delivery.interval}s period"
            )


@dataclass(frozen=True, slots=True)
class Unverified:
    """配置已保存，但尚无真实设备测量值。"""


Capability = Measured | Unverified
"""连接器能力；未验证是显式状态，不是缺失值。"""


class PointRole(Enum):
    """模板要求点位承担的角色。"""

    START_SIGNAL = "start_signal"
    END_SIGNAL = "end_signal"
    ORDERED_STEP = "ordered_step"
    UNORDERED_STEP = "unordered_step"
    SAFETY_OUTPUT = "safety_output"


class Unfitness(Enum):
    """能力声明无法承担角色的具体原因。"""

    CAPABILITY_UNVERIFIED = "capability_unverified"
    MAY_DROP_EDGES = "may_drop_edges"
    NOT_SEQUENCED = "not_sequenced"
    DELIVERY_TOO_SLOW = "delivery_too_slow"


def unfit_for(capability: Capability, *, role: PointRole, budget: float) -> tuple[Unfitness, ...]:
    """按稳定顺序返回该能力不能承担角色的全部原因。"""
    if budget < 0:
        raise ValueError(f"a role budget cannot be negative, got {budget}")
    if isinstance(capability, Unverified):
        return (Unfitness.CAPABILITY_UNVERIFIED,)

    found: list[Unfitness] = []
    match role:
        case (
            PointRole.START_SIGNAL
            | PointRole.END_SIGNAL
            | PointRole.ORDERED_STEP
            | PointRole.UNORDERED_STEP
        ):
            if capability.edges is EdgePreservation.MAY_DROP:
                found.append(Unfitness.MAY_DROP_EDGES)
        case PointRole.SAFETY_OUTPUT:
            # 输出赋值不读取输入边沿或到达顺序，只读取实测状态与执行延迟。
            pass
        case _:
            assert_never(role)
    if role is PointRole.ORDERED_STEP and capability.sequencing is Sequencing.UNSEQUENCED:
        found.append(Unfitness.NOT_SEQUENCED)
    if capability.max_delivery_delay > budget:
        found.append(Unfitness.DELIVERY_TOO_SLOW)
    return tuple(found)


def capability_to_wire(capability: Capability) -> dict[str, str | float | None]:
    """把能力声明转为中心持久化和跨进程传输共用的稳定对象。"""
    if isinstance(capability, Unverified):
        return {"verification": "unverified"}
    if isinstance(capability, Measured):
        if isinstance(capability.delivery, Pushed):
            delivery = "pushed"
            interval = None
        elif isinstance(capability.delivery, Polled):
            delivery = "polled"
            interval = capability.delivery.interval
        else:
            assert_never(capability.delivery)
        return {
            "verification": "measured",
            "delivery": delivery,
            "polling_interval_seconds": interval,
            "max_delivery_delay_seconds": capability.max_delivery_delay,
            "sequencing": capability.sequencing.value,
            "edge_preservation": capability.edges.value,
            "timestamp_source": capability.timestamps.value,
        }
    assert_never(capability)


def capability_from_wire(value: Mapping[str, object]) -> Capability:
    """从严格对象恢复能力声明；未知或半套实测值不会被猜成有效能力。"""
    verification = value.get("verification")
    if verification == "unverified":
        if set(value) != {"verification"}:
            raise ValueError("an unverified capability cannot carry measured fields")
        return Unverified()
    if verification != "measured":
        raise ValueError("capability verification is not supported")

    required = {
        "verification",
        "delivery",
        "polling_interval_seconds",
        "max_delivery_delay_seconds",
        "sequencing",
        "edge_preservation",
        "timestamp_source",
    }
    if set(value) != required:
        raise ValueError("a measured capability must carry every declaration")

    delivery_name = value["delivery"]
    interval_value = value["polling_interval_seconds"]
    if delivery_name == "pushed":
        if interval_value is not None:
            raise ValueError("a pushed capability cannot carry a polling interval")
        delivery: Delivery = Pushed()
    elif delivery_name == "polled":
        delivery = Polled(interval=_number(interval_value, "polling_interval_seconds"))
    else:
        raise ValueError("capability delivery is not supported")

    try:
        sequencing = Sequencing(value["sequencing"])
        edges = EdgePreservation(value["edge_preservation"])
        timestamps = TimestampSource(value["timestamp_source"])
    except (TypeError, ValueError) as error:
        raise ValueError("capability declaration contains an unsupported value") from error
    return Measured(
        delivery=delivery,
        max_delivery_delay=_number(
            value["max_delivery_delay_seconds"], "max_delivery_delay_seconds"
        ),
        sequencing=sequencing,
        edges=edges,
        timestamps=timestamps,
    )


def _number(value: object, field: str) -> float:
    """只接受 JSON 数字，不把布尔值当作时长。"""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field} must be a number")
    return float(value)
