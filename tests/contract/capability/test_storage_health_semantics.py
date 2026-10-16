"""Edge 实际生产的存储健康事实 -> 固定 wire 消费语义的契约回归。

§5.21 的 wire 语义是固定基线: ``delivering`` 是唯一的健康取值, 未知状态保守地按
受损处理。本契约在公开 supervisor/传输/序列化边界上生成真实消息, 按这份独立的
固定预期断言, 不跟随 Center 当前实现, 也不运行旧版本依赖。
"""

from __future__ import annotations

import json
import unittest

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

from nvsop_contracts import (
    ConfigurationBundle,
    ReportedDecision,
    ReportedHealth,
    ReportedObservation,
    ReportedSopInstance,
)

HOST_ID = "019937d8-0d10-7b31-8d2d-4e60c8f4f211"
STATION_ID = "019937d8-0d10-7b31-8d2d-4e60c8f4f212"
MARGINS = EvidenceMargins(leading=5.0, trailing=5.0)


class HealthWireCollector:
    """按 DecisionReportTransport 协议抓取 wire 报文的传输接缝。

    本契约只生产健康事实; 其它报文类型意味着上游行为超出契约对象, 明确失败而不
    静默吞掉。
    """

    def __init__(self) -> None:
        self.health_reports: list[dict[str, object]] = []

    def send_health(
        self, report: ReportedHealth, *, configuration: ConfigurationBundle | None
    ) -> None:
        self.health_reports.append(report.to_wire())

    def send_decision(
        self, report: ReportedDecision, *, configuration: ConfigurationBundle | None
    ) -> None:
        raise AssertionError(f"contract only produces health facts, got decision {report.event_id}")

    def send_observation(self, report: ReportedObservation) -> None:
        raise AssertionError(
            f"contract only produces health facts, got observation {report.event_id}"
        )

    def send_instance(
        self, report: ReportedSopInstance, *, configuration: ConfigurationBundle | None
    ) -> None:
        raise AssertionError(f"contract only produces health facts, got instance {report.event_id}")


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


class StorageHealthSemanticsContractTest(unittest.TestCase):
    def test_recovery_is_delivering_and_failure_is_storage_unavailable(self) -> None:
        state = open_local_state(":memory:")
        self.addCleanup(state.close)
        context = ReportContext(HOST_ID, STATION_ID, (), None, None, None, None, None)
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
        transport = HealthWireCollector()
        attempts = HostReportReconciler(reports=state.reports(), transport=transport).flush(
            now=HostInstant(clock[0] + 1),
            reported_at="2026-09-16T00:00:00+00:00",
        )
        self.assertTrue(all(attempt.sent for attempt in attempts))
        (failure, recovery) = transport.health_reports

        # 受支持的 wire 语义里 ``delivering`` 是唯一的健康取值: 恢复必须以
        # delivering 表达, 失败必须以 storage_unavailable + 原因码表达, 任何
        # 其它取值都会被保守地按受损处理, 永远无法恢复。
        self.assertEqual("storage_unavailable", failure["status"])
        self.assertEqual("STORAGE_UNAVAILABLE", failure["reason_code"])
        self.assertEqual("delivering", recovery["status"])
        self.assertIsNone(recovery["reason_code"])

        detail_wire = failure["detail"]
        if not isinstance(detail_wire, str):
            self.fail("storage health detail must be JSON text")
        detail = json.loads(detail_wire)
        self.assertEqual("recording", detail["role"])
        self.assertEqual(90.0, detail["high_watermark_percent"])
        self.assertEqual(80.0, detail["recovery_watermark_percent"])


if __name__ == "__main__":
    unittest.main()
