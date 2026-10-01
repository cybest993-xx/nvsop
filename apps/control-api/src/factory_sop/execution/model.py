"""execution 拥有的当前物理执行权租约与强制改绑确认事实。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

# 强制改绑通道的固定风险原文。服务器权威：确认输入必须与它一致，客户端不能自拟一段
# 说明冒充已读；两侧确认各自记录“警示已展示”事实。
HANDOVER_RISK_STATEMENT = "若旧机仍在运行将造成重复物理写入"


@dataclass(frozen=True, slots=True)
class StationGrant:
    """一个工位当前由中心确认的物理执行权租约。"""

    grant_id: UUID
    station_id: UUID
    holder_host_id: UUID
    lease_expires_at: datetime
    renewed_at: datetime
    request_id: UUID


@dataclass(frozen=True, slots=True)
class HandoverConfirmation:
    """一次强制改绑请求及其两人确认事实。

    内容（工位、旧机、目标机）在建立时冻结，改内容只能新建请求；第二人按请求 id 与
    被展示的同一内容确认，旧确认不随内容搬移。`operator_*` 是操作者首确认，`second_*`
    是另一名当前有强制改绑权限的用户第二确认；两者都记录身份、时刻与风险展示事实。
    """

    handover_id: UUID
    station_id: UUID
    from_host_id: UUID
    to_host_id: UUID
    operator_id: UUID
    operator_confirmed_at: datetime
    operator_risk_shown: bool
    second_operator_id: UUID | None = None
    second_confirmed_at: datetime | None = None
    second_risk_shown: bool | None = None


__all__ = ["HANDOVER_RISK_STATEMENT", "HandoverConfirmation", "StationGrant"]
