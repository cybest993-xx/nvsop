"""推理机独立的证据引用上报: 中心只确认登记, 不接收或拥有媒体字节。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from time import monotonic, sleep

from edge_runtime.center_client import CenterClient
from edge_runtime.evidence_media import evidence_registration_payloads
from edge_runtime.judgment.model import HostInstant
from edge_runtime.local_state import LocalState

REGISTRATION_PATH = "/api/v1/evidence/registrations"


class EvidenceReferenceReconciler:
    """从 SQLite 本机积压跨工位上报, 丢确认后依靠中心稳定 ID 幂等对账。"""

    def __init__(
        self,
        *,
        state: LocalState,
        client: CenterClient,
        host_id: str,
        evidence_directory: Path,
        interval: float,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if interval <= 0:
            raise ValueError("evidence report interval must be positive")
        self._state = state
        self._client = client
        self._host_id = host_id
        self._directory = evidence_directory
        self._interval = interval
        self._clock = clock

    def drain(self, *, limit: int) -> int:
        """只在每个实际媒体引用均被中心确认后完成一条待办, 失败保留原行。"""
        completed = 0
        remaining = limit
        for station_id in self._state.pending_evidence_registration_stations():
            if remaining <= 0:
                break
            station = self._state.station(station_id)
            for pending in station.evidence_ready_for_registration(limit=remaining):
                if remaining <= 0:
                    break
                if pending.sliced_at is None or pending.media_results is None:
                    continue
                if (
                    pending.covered_from is None
                    or pending.covered_to is None
                    or pending.covered_from > pending.start.seconds
                    or pending.covered_to < pending.end.seconds
                ):
                    continue
                remaining -= 1
                now = HostInstant(self._clock())
                try:
                    payloads = evidence_registration_payloads(
                        pending,
                        host_id=self._host_id,
                        station_id=station_id,
                        directory=self._directory,
                    )
                    for payload in payloads:
                        response = self._client.post(REGISTRATION_PATH, payload)
                        if response.status not in (200, 201):
                            raise ValueError(
                                f"center registration rejected: HTTP {response.status}"
                            )
                        ack = json.loads(response.body)
                        if (
                            not isinstance(ack, dict)
                            or ack.get("accepted") is not True
                            or ack.get("evidence_id") != payload["evidence_id"]
                            or ack.get("status") != "available"
                        ):
                            raise ValueError("center did not confirm the registered reference")
                    if station.mark_evidence_registered(
                        pending.queue_id, at=now, expected_media_results=pending.media_results
                    ):
                        completed += 1
                except Exception as error:
                    # 中心链路失败不能影响自治判定; 记录本条失败并保留 SQLite 待办及本机媒体。
                    station.record_evidence_failure(
                        pending.queue_id,
                        at=now,
                        error=f"{type(error).__name__}: {error}"[:255],
                    )
        return completed

    def run_forever(self, *, should_stop: Callable[[], bool], limit: int) -> None:
        while not should_stop():
            self.drain(limit=limit)
            deadline = monotonic() + self._interval
            while not should_stop() and monotonic() < deadline:
                sleep(0.05)


__all__ = ["EvidenceReferenceReconciler"]
