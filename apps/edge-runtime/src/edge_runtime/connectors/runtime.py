"""每个已配置连接器的运行时和能力门。"""

from __future__ import annotations

from collections.abc import Mapping
from time import monotonic

from nvsop_contracts import Measured, Polled, Pushed, Unverified

from edge_runtime.connectors.port import Connector, InputPoint
from edge_runtime.connectors.watch import ConnectorPoller
from edge_runtime.supervisor.inputs import SupervisorInput


class ConnectorRuntime:
    """一个已配置连接器拥有的唯一运行时。

    ``Unverified`` 是真实的配置状态,因此不能产生轮询副作用。当前运行时没有推送订阅实现,
    所以构造时拒绝 ``Pushed``;静默把它当作轮询器会把能力声明变成猜测。
    """

    def __init__(
        self,
        *,
        connector_id: str,
        connector: Connector,
        input_points: tuple[InputPoint, ...],
        timeout: float,
    ) -> None:
        if not connector_id:
            raise ValueError("connector_id must not be empty")
        if timeout <= 0:
            raise ValueError("connector timeout must be positive")
        self.connector_id = connector_id
        self._connector = connector
        self._poller: ConnectorPoller | None
        self._next_due_at: float | None = None
        capability = connector.capability
        if isinstance(capability, Unverified):
            self.polling_interval: float | None = None
            self._poller = None
        elif isinstance(capability, Measured) and isinstance(capability.delivery, Polled):
            self.polling_interval = capability.delivery.interval
            self._poller = ConnectorPoller(
                connector=connector,
                points=input_points,
                timeout=timeout,
            )
        elif isinstance(capability, Measured) and isinstance(capability.delivery, Pushed):
            raise ValueError("a pushed connector requires a push runtime and cannot be polled")
        else:
            raise AssertionError(f"unsupported connector capability: {capability!r}")

    @property
    def connector(self) -> Connector:
        """返回该运行时拥有的适配器,供输出 dispatcher 接缝使用。"""
        return self._connector

    def next_due(self, *, now: float) -> float | None:
        """返回下一次轮询截止点;未验证连接器永远不会到期。"""
        if self.polling_interval is None:
            return None
        if self._next_due_at is None:
            self._next_due_at = now
        return self._next_due_at

    def poll(self, *, now: float | None = None) -> tuple[SupervisorInput, ...]:
        """轮询一次并推进该运行时的 cadence。"""
        if self._poller is None:
            return ()
        arriving = self._poller.poll()
        assert self.polling_interval is not None
        self._next_due_at = (monotonic() if now is None else now) + self.polling_interval
        return arriving


class ConnectorRuntimeSet:
    """为每个已配置连接器身份准确构造一个运行时。"""

    def __init__(
        self,
        *,
        connectors: Mapping[str, Connector],
        input_points: Mapping[str, tuple[InputPoint, ...]],
        timeout: float,
    ) -> None:
        runtimes: dict[str, ConnectorRuntime] = {}
        for connector_id, connector in connectors.items():
            if connector_id in runtimes:
                raise ValueError(f"connector {connector_id!r} has more than one runtime")
            runtimes[connector_id] = ConnectorRuntime(
                connector_id=connector_id,
                connector=connector,
                input_points=input_points.get(connector_id, ()),
                timeout=timeout,
            )
        self._runtimes = runtimes

    def runtime(self, connector_id: str) -> ConnectorRuntime:
        try:
            return self._runtimes[connector_id]
        except KeyError as error:
            raise KeyError(f"connector runtime is not configured: {connector_id}") from error

    @property
    def runtimes(self) -> tuple[ConnectorRuntime, ...]:
        return tuple(self._runtimes.values())

    def next_due(self, connector_id: str, *, now: float) -> float | None:
        return self.runtime(connector_id).next_due(now=now)

    def poll(self, connector_id: str, *, now: float | None = None) -> tuple[SupervisorInput, ...]:
        return self.runtime(connector_id).poll(now=now)


__all__ = ["ConnectorRuntime", "ConnectorRuntimeSet"]
