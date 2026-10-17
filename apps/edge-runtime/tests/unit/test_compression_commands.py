"""#212 本机压缩控制轮的传输语义: 中心是租约权威, 拒绝即交回队列。"""

from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from unittest import mock

from edge_runtime import compression_commands
from edge_runtime.center_client import CenterClient, CenterResponse
from edge_runtime.compression_commands import CompressionCommandLoop
from edge_runtime.recording_compression import CompressionStatus
from edge_runtime.supervisor.delegated_transport import CommandTransportError

NEXT = "/api/v1/device-commands/compression/next"
COMMAND = "019937d8-0d10-7b31-8d2d-4e60c8f4f401"


class FakeCenterClient(CenterClient):
    """只在测试内替换 HTTP; 领取与回报的响应由用例逐次给定。"""

    def __init__(self) -> None:
        self.claim_status = 200
        self.lease: dict[str, object] | None = None
        self.reports: list[int] = []
        self.gets = 0
        self.posts: list[tuple[str, dict[str, object], dict[str, str]]] = []

    def get(self, path: str) -> CenterResponse:
        self.gets += 1
        assert path == NEXT
        if self.claim_status != 200:
            return CenterResponse(self.claim_status, b"")
        lease = self.lease if self.lease is not None else {"lease_expires_at": 0.0}
        document = {
            "command_type": "control_recording_compression",
            "command_id": COMMAND,
            **lease,
        }
        return CenterResponse(200, json.dumps(document).encode())

    def post(
        self,
        path: str,
        body: Mapping[str, object],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> CenterResponse:
        self.posts.append((path, dict(body), dict(headers or {})))
        return CenterResponse(self.reports.pop(0), b"")


class FakeWorker:
    """本机压缩 worker 的控制面; 状态由用例逐次给定。"""

    readrate = 0.25

    def __init__(
        self,
        state: str = "idle",
        *,
        transient: bool = False,
        detail: str | None = None,
        stuck: bool = False,
    ) -> None:
        self.state = state
        self.requested = "run"
        self.calls: list[str] = []
        self._transient = transient
        self._stuck = stuck
        self.detail = detail

    def status(self) -> CompressionStatus:
        return CompressionStatus(
            self.state, self.requested, "2026-10-17_12-00-00.mp4", None, self.detail
        )

    def pause(self) -> CompressionStatus:
        self.calls.append("pause")
        if self._stuck:
            return self.status()
        self.requested = "pause"
        self.state = "pausing" if self._transient else "paused"
        return self.status()

    def resume(self) -> CompressionStatus:
        self.calls.append("resume")
        if self._stuck:
            return self.status()
        self.requested = "run"
        self.state = "resuming" if self._transient else "idle"
        return self.status()

    def abort(self) -> CompressionStatus:
        self.calls.append("abort")
        if self._stuck:
            return self.status()
        self.requested = "abort"
        self.state = "aborting" if self._transient else "aborted"
        return self.status()


def _claim(action: str, *, lease_expires_at: float) -> dict[str, object]:
    return {
        "action": action,
        "claim_token": "claim-token",
        "lease_expires_at": lease_expires_at,
    }


class CompressionCommandLoopTests(unittest.TestCase):
    def loop(self, worker: FakeWorker | None) -> tuple[CompressionCommandLoop, FakeCenterClient]:
        client = FakeCenterClient()
        return (
            CompressionCommandLoop(client=client, worker=worker, interval=0.01),
            client,
        )

    def test_center_epoch_is_compared_by_nobody_but_the_center(self) -> None:
        """本机时钟落后于中心 epoch 时仍要执行并回报, 不用第二个过期规则丢弃领取。"""
        loop, client = self.loop(FakeWorker())
        client.lease = _claim("pause", lease_expires_at=1.0)
        client.reports = [200]

        assert loop.run_once() is True
        assert client.gets == 1
        path, body, headers = client.posts[0]
        assert path == f"/api/v1/device-commands/compression/{COMMAND}/result"
        assert body["state"] == "paused"
        assert headers["X-Command-Claim-Token"] == "claim-token"

    def test_rejected_report_returns_the_command_to_the_queue(self) -> None:
        """中心 4xx 说明这次领取已不适用: 不再重放陈旧观测, 下一轮重新领取。"""
        loop, client = self.loop(FakeWorker())
        client.lease = _claim("pause", lease_expires_at=1.0)
        client.reports = [409, 200]

        with self.assertRaises(CommandTransportError):
            loop.run_once()
        assert loop.run_once() is True
        assert client.gets == 2
        assert len(client.posts) == 2

    def test_server_error_keeps_the_same_observation_until_accepted(self) -> None:
        """中心 5xx 属于传输失败: 保留同一观测, 不重新领取也不重新执行。"""
        worker = FakeWorker()
        loop, client = self.loop(worker)
        client.lease = _claim("abort", lease_expires_at=1.0)
        client.reports = [503, 200]

        with self.assertRaises(CommandTransportError):
            loop.run_once()
        assert loop.run_once() is True
        assert client.gets == 1
        assert [body for _, body, _ in client.posts] == [client.posts[0][1]] * 2
        assert worker.calls == ["abort"]

    def test_unconfirmed_transient_state_reports_control_unconfirmed(self) -> None:
        """进程状态在确认等待内没有落定, 回报的是未确认而不是成功。"""
        loop, client = self.loop(FakeWorker(transient=True))
        client.lease = _claim("pause", lease_expires_at=1.0)
        client.reports = [200]

        with mock.patch.object(compression_commands, "_CONTROL_CONFIRM_SECONDS", 0.05):
            assert loop.run_once() is True
        _, body, _ = client.posts[0]
        assert body["state"] == "pausing"
        assert body["failure_code"] == "control_unconfirmed"

    def test_unconfirmed_report_keeps_the_yield_reason_visible(self) -> None:
        """未确认时仍要保留本机观测原因, 否则运维看不到是判定积压让路而不是故障。"""
        worker = FakeWorker(
            "paused", detail="auto-yield: judgment backlog in observation", stuck=True
        )
        loop, client = self.loop(worker)
        client.lease = _claim("resume", lease_expires_at=1.0)
        client.reports = [200]

        assert loop.run_once() is True
        _, body, _ = client.posts[0]
        assert body["failure_code"] == "control_unconfirmed"
        assert worker.calls == ["resume"]
        assert str(body["detail"]).endswith("auto-yield: judgment backlog in observation")

    def test_absent_worker_reports_unavailable_instead_of_silence(self) -> None:
        """未启用压缩的本机也要给出可诊断的回报, 不留待领取命令。"""
        loop, client = self.loop(None)
        client.lease = _claim("inspect", lease_expires_at=1.0)
        client.reports = [200]

        assert loop.run_once() is True
        _, body, _ = client.posts[0]
        assert body["state"] == "unavailable"
        assert body["failure_code"] == "compression_unavailable"

    def test_inspect_reports_auto_yield_reason_through_the_existing_detail_field(self) -> None:
        """判定积压让路的原因必须能从已有命令观测读到, 不需要新增契约字段。"""
        worker = FakeWorker(
            "paused", detail="auto-yield: judgment backlog in observation", stuck=True
        )
        loop, client = self.loop(worker)
        client.lease = _claim("inspect", lease_expires_at=1.0)
        client.reports = [200]

        assert loop.run_once() is True
        _, body, _ = client.posts[0]
        assert body["state"] == "paused"
        assert body["detail"] == "auto-yield: judgment backlog in observation"
        assert worker.calls == []


if __name__ == "__main__":
    unittest.main()
