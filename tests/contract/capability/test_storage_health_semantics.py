"""Edge 实际生产的存储健康事实 -> 受支持旧 Center 消费者语义的契约回归。

Issue #209 初版把恢复也上报为 ``storage_healthy``; 当前受支持的旧 Center 只认
``delivering`` 为健康 (§5.21: 未知状态保守地按受损处理), 因此当时代码与旧消费
方同版本发布导致测试漏检。本契约在公开 supervisor/传输/序列化边界上生成真实
消息, 期望值从独立固定的 §5.21 语义基线推导, 不跟随 Center 当前实现。
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from uuid import UUID

from edge_runtime.judgment.evidence import EvidenceMargins
from edge_runtime.judgment.model import (
    HostInstant,
    JudgmentState,
    Ordering,
    RuntimeParameters,
    Template,
)
from edge_runtime.local_state import ReportContext, open_local_state
from edge_runtime.reporting import HostReportReconciler
from edge_runtime.storage_health import StorageHealthEvent
from edge_runtime.supervisor.inputs import StorageHealthObserved
from edge_runtime.supervisor.station import StationSupervisor

from factory_sop.monitor.usecases import mirror_health
from nvsop_contracts import reported_health_from_wire

HOST_ID = UUID("019937d8-0d10-7b31-8d2d-4e60c8f4f211")
STATION_ID = "019937d8-0d10-7b31-8d2d-4e60c8f4f212"
"""Center 消费侧把工位身份解析为 UUID, 所以契约用 UUID 形态的工位标识。"""
MARGINS = EvidenceMargins(leading=5.0, trailing=5.0)


class OwnedHostGateway:
    """该主机当前拥有此工位; 归属拓扑不是本契约的对象。"""

    def owns_station(self, *, host_id: UUID, station_id: UUID) -> bool:
        return host_id == HOST_ID

    def has_historical_station(
        self,
        *,
        host_id: UUID,
        station_id: UUID,
        template_version_id: str | None = None,
        template_sha256: str | None = None,
    ) -> bool:
        return host_id == HOST_ID


class MonitorMirror:
    def __init__(self) -> None:
        self.health: list[object] = []

    def upsert_health(self, value: object) -> bool:
        self.health.append(value)
        return True


class MirrorTransport:
    """edge 报告传输接缝: 抓取 wire 报文并经真实 mirror 走旧 Center 消费语义。"""

    def __init__(self, mirror: MonitorMirror) -> None:
        self.mirror = mirror
        self.wire_reports: list[dict[str, object]] = []

    def send_health(self, report: object, *, configuration: object) -> None:
        wire = report.to_wire()
        self.wire_reports.append(wire)
        mirror_health(
            reported_health_from_wire(wire),
            received_at=datetime(2026, 9, 16, tzinfo=UTC),
            monitor=self.mirror,  # type: ignore[arg-type]
            host_gateway=OwnedHostGateway(),
            device_gateway=OwnedHostGateway(),
        )


def observed(role: str, *, impaired: bool) -> StorageHealthObserved:
    return StorageHealthObserved(
        StorageHealthEvent(
            role=role,
            path=f"/synthetic/{role}",
            impaired=impaired,
            used_percent=91.0 if impaired else 75.0,
            total_bytes=100,
            available_bytes=9 if impaired else 25,
            high_watermark_percent=90.0,
            recovery_watermark_percent=80.0,
            error=None,
        )
    )


def supported_old_center_status(mirrored: object) -> str:
    """旧 Center 的受支持消费语义 (§5.21): 只有 ``delivering`` 算健康。"""
    return mirrored.report.status


class StorageHealthSemanticsContractTest(unittest.TestCase):
    def test_recovery_is_delivering_and_failure_is_storage_unavailable(self) -> None:
        state = open_local_state(":memory:")
        self.addCleanup(state.close)
        context = ReportContext(str(HOST_ID), STATION_ID, (), None, None, None, None, None)
        store = state.station(STATION_ID, report_context=context)
        clock = [1_700_000_000.0]
        supervisor = StationSupervisor(
            state=JudgmentState(
                template=Template(
                    steps=("step-1",), ordering=Ordering.ORDERED, start_signal="step-1"
                ),
                parameters=RuntimeParameters(idle_timeout=300.0, step_deadline=60.0),
            ),
            store=store,
            margins=MARGINS,
            clock=lambda: clock[0],
        )

        # 磁盘越过高水位 -> 失效事实; 回到恢复水位以下 -> 恢复事实。
        supervisor.receive(observed("recording", impaired=True))
        supervisor.receive(observed("recording", impaired=False))

        self.assertEqual(2, len(store.pending_health_reports()))
        mirror = MonitorMirror()
        transport = MirrorTransport(mirror)
        attempts = HostReportReconciler(reports=state.reports(), transport=transport).flush(
            now=HostInstant(clock[0] + 1),
            reported_at="2026-09-16T00:00:00+00:00",
        )
        self.assertTrue(all(attempt.sent for attempt in attempts))
        self.assertEqual(2, len(transport.wire_reports))

        # 旧 Center 只把 delivering 判为健康: 恢复必须以 delivering 表达,
        # 失败必须以 storage_unavailable + 原因码表达, 任何其它取值都会被
        # 保守地按受损处理, 永远无法恢复。
        self.assertEqual("storage_unavailable", supported_old_center_status(mirror.health[0]))
        self.assertEqual("STORAGE_UNAVAILABLE", mirror.health[0].report.reason_code)
        self.assertEqual("delivering", supported_old_center_status(mirror.health[1]))
        self.assertIsNone(mirror.health[1].report.reason_code)
        self.assertEqual("recording", json_stream_role(transport.wire_reports))


def json_stream_role(wire_reports: list[dict[str, object]]) -> str:
    import json

    detail = json.loads(wire_reports[-1]["detail"])
    return detail["role"]


if __name__ == "__main__":
    unittest.main()
