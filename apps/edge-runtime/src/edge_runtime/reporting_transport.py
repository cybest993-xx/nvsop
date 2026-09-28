"""monitor 判定与健康上报的带签名 HTTP 传输。"""

from __future__ import annotations

import json
from collections.abc import Mapping

from nvsop_contracts import (
    DECISION_REPORT_CONTRACT_VERSION,
    REPORT_CAPABILITIES_HEADER,
    SOP_INSTANCE_REPORT_CAPABILITY,
    SOP_INSTANCE_REPORT_CONTRACT_VERSION,
    ConfigurationBundle,
    ReportedDecision,
    ReportedHealth,
    ReportedSopInstance,
    configuration_to_wire,
    reported_decision_to_wire,
    reported_health_to_wire,
    reported_sop_instance_to_wire,
)

from edge_runtime.center_client import CenterClient, CenterUnreachableError
from edge_runtime.reporting import DecisionReportTransport


class ReportTransportError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class HttpDecisionReportTransport(DecisionReportTransport):
    """至少一次 POST 传输;是否重试由本地队列决定。"""

    def __init__(self, *, client: CenterClient, host_id: str) -> None:
        self._client = client
        self._host_id = host_id
        self._confirmed_report_configurations: set[tuple[int, str]] = set()

    def send_decision(
        self,
        report: ReportedDecision,
        *,
        configuration: ConfigurationBundle | None,
    ) -> None:
        if report.host_id != self._host_id:
            raise ValueError("a report cannot be sent by a different host")
        if report.contract_version == DECISION_REPORT_CONTRACT_VERSION:
            if configuration is None:
                raise ValueError("v2 report requires its frozen confirmed configuration")
            self._ensure_report_compatibility(
                host_id=report.host_id,
                configuration_revision=report.configuration_revision,
                configuration_sha256=report.configuration_sha256,
                configuration=configuration,
            )
        elif configuration is not None:
            raise ValueError("v1 report cannot carry a confirmed configuration proof")
        self._post("/api/v1/monitor/reported-decisions", reported_decision_to_wire(report))

    def _ensure_report_compatibility(
        self,
        *,
        host_id: str,
        configuration_revision: int | None,
        configuration_sha256: str | None,
        configuration: ConfigurationBundle,
    ) -> None:
        if (
            configuration.host_id != host_id
            or configuration.config_revision != configuration_revision
            or configuration.effective_sha256 != configuration_sha256
        ):
            raise ValueError("frozen configuration does not match report proof")
        key = (configuration.config_revision, configuration.effective_sha256)
        if key in self._confirmed_report_configurations:
            return
        path = f"/api/v1/inference-hosts/{self._host_id}/confirmed-configuration"
        response = self._post_json(
            path,
            configuration_to_wire(configuration),
            extra_headers={REPORT_CAPABILITIES_HEADER: SOP_INSTANCE_REPORT_CAPABILITY},
        )
        if set(response) != {
            "decision_report_contract_version",
            "sop_instance_report_contract_version",
        }:
            raise ReportTransportError("中心运行时兼容响应格式不受支持")
        if response["decision_report_contract_version"] != DECISION_REPORT_CONTRACT_VERSION:
            raise ReportTransportError("中心不支持当前 historical decision report contract")
        if response["sop_instance_report_contract_version"] != SOP_INSTANCE_REPORT_CONTRACT_VERSION:
            raise ReportTransportError("中心不支持当前 SOP instance report contract")
        self._confirmed_report_configurations.add(key)

    def send_health(self, report: ReportedHealth) -> None:
        if report.host_id != self._host_id:
            raise ValueError("a health report cannot be sent by a different host")
        self._post("/api/v1/monitor/health", reported_health_to_wire(report))

    def send_instance(
        self,
        report: ReportedSopInstance,
        *,
        configuration: ConfigurationBundle | None,
    ) -> None:
        if report.host_id != self._host_id:
            raise ValueError("an instance report cannot be sent by a different host")
        if configuration is None:
            raise ValueError("instance report requires its frozen confirmed configuration")
        self._ensure_report_compatibility(
            host_id=report.host_id,
            configuration_revision=report.configuration_revision,
            configuration_sha256=report.configuration_sha256,
            configuration=configuration,
        )
        self._post("/api/v1/monitor/reported-instances", reported_sop_instance_to_wire(report))

    def _post(self, path: str, body: dict[str, object]) -> None:
        try:
            response = self._client.post(path, body)
        except CenterUnreachableError as error:
            raise ReportTransportError("中心 monitor 接口暂时不可达") from error
        if response.status not in {200, 201, 204}:
            raise ReportTransportError("中心 monitor 接口返回非成功状态", status=response.status)

    def _post_json(
        self,
        path: str,
        body: dict[str, object],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> Mapping[str, object]:
        try:
            response = self._client.post(path, body, headers=extra_headers)
        except CenterUnreachableError as error:
            raise ReportTransportError("中心兼容握手暂时不可达") from error
        if response.status >= 400:
            raise ReportTransportError(
                "中心不支持当前 historical decision report compatibility handshake",
                status=response.status,
            )
        if response.status != 200:
            raise ReportTransportError("中心兼容握手返回非成功状态", status=response.status)
        try:
            value = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ReportTransportError("中心兼容握手响应不是有效 JSON") from error
        if not isinstance(value, Mapping):
            raise ReportTransportError("中心兼容握手响应不是对象")
        return value


__all__ = ["HttpDecisionReportTransport", "ReportTransportError"]
