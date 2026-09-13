"""判定证据的配置余量和已解析窗口, 不执行切片或持久化。"""

from dataclasses import dataclass

from edge_runtime.judgment.model import HostInstant


@dataclass(frozen=True, slots=True)
class EvidenceMargins:
    """判定锚点前后的证据余量, 单位为秒。"""

    leading: float
    trailing: float

    def __post_init__(self) -> None:
        if self.leading < 0 or self.trailing < 0:
            raise ValueError(
                f"evidence margins cannot be negative, got {self.leading}/{self.trailing}"
            )


@dataclass(frozen=True, slots=True)
class EvidenceClip:
    """一个实例锚点对应的完整证据窗口。"""

    instance_id: int
    anchor: HostInstant
    start: HostInstant
    end: HostInstant
