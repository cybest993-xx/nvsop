"""`device` 的小型跨模块契约。

本切片中，模板模块需要在工作簿导入时按自然编码解析工位。其余 CRUD 用例仍只对本模块
的 HTTP 适配器和直接调用方开放；把它们重新导出会形成第二个透传入口，违反仓库规则 §3
和控制面 §5.16。

其他模块需要设备行为时，只添加调用方真正使用的具名深层操作，并在这里说明角色和调用方式。
实现仍在 `usecases/`，持久化仍在仓储 seam 后；本文件只定义跨模块契约。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from factory_sop.device.model import Station


class StationCodeLookup(Protocol):
    """设备模块为自然编码查询提供的最小存储 seam。"""

    def by_code(self, code: str) -> Station | None:
        """返回编码匹配的工位。"""
        ...


@dataclass(frozen=True, slots=True)
class StationReference:
    """供其他模块做自然编码匹配的最小工位身份。"""

    id: UUID
    code: str
    name: str


def station_by_code(*, code: str, stations: StationCodeLookup) -> StationReference | None:
    """按工位编码返回最小身份；模板导入用它验证工作簿引用。"""
    station: Station | None = stations.by_code(code)
    if station is None:
        return None
    return StationReference(id=station.id, code=station.code, name=station.name)


__all__ = ["StationCodeLookup", "StationReference", "station_by_code"]
