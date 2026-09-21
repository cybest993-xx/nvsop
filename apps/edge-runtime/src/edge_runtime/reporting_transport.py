"""monitor 判定与健康上报的带签名 HTTP 传输。"""

from __future__ import annotations

import json
import secrets
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Protocol, cast

from nvsop_contracts import (
    DECISION_REPORT_CONTRACT_VERSION,
    REPORT_CAPABILITIES_HEADER,
    SOP_INSTANCE_REPORT_CAPABILITY,
    SOP_INSTANCE_REPORT_CONTRACT_VERSION,
    ConfigurationBundle,
    HostIdentityRequest,
    ReportedDecision,
    ReportedHealth,
    ReportedSopInstance,
    configuration_to_wire,
    reported_decision_to_wire,
    reported_health_to_wire,
    reported_sop_instance_to_wire,
    sign_host_identity_request,
)

from edge_runtime.reporting import DecisionReportTransport


class ReportTransportError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class HttpDecisionReportTransport(DecisionReportTransport):
    """至少一次 POST 传输;是否重试由本地队列决定。"""

    def __init__(
        self,
        *,
        center_url: str,
        host_id: str,
        host_private_key: str,
        timeout: float,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if not center_url or not host_id or not host_private_key:
            raise ValueError("report transport identity and URL must not be empty")
        if timeout <= 0:
            raise ValueError("report transport timeout must be positive")
        self._base_url = center_url.rstrip("/")
        self._host_id = host_id
        self._host_private_key = host_private_key
        self._timeout = timeout
        self._ssl_context = ssl_context
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
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                **self._signed_headers(path, body),
            },
            method="POST",
        )
        try:
            response = cast(
                _HttpResponse,
                urllib.request.urlopen(
                    request,
                    timeout=self._timeout,
                    context=self._ssl_context,
                ),
            )
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            raise ReportTransportError("中心 monitor 接口拒绝回报", status=status) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise ReportTransportError("中心 monitor 接口暂时不可达") from error
        try:
            if response.status not in {200, 201, 204}:
                raise ReportTransportError(
                    "中心 monitor 接口返回非成功状态", status=response.status
                )
        finally:
            response.close()

    def _post_json(
        self,
        path: str,
        body: dict[str, object],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> Mapping[str, object]:
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                **self._signed_headers(path, body),
                **({} if extra_headers is None else dict(extra_headers)),
            },
            method="POST",
        )
        try:
            response = cast(
                _HttpResponse,
                urllib.request.urlopen(
                    request,
                    timeout=self._timeout,
                    context=self._ssl_context,
                ),
            )
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            raise ReportTransportError(
                "中心不支持当前 historical decision report compatibility handshake",
                status=status,
            ) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise ReportTransportError("中心兼容握手暂时不可达") from error
        try:
            if response.status != 200:
                raise ReportTransportError("中心兼容握手返回非成功状态", status=response.status)
            try:
                raw = response.read()
            except (OSError, TimeoutError) as error:
                raise ReportTransportError("中心兼容握手响应读取失败") from error
        finally:
            response.close()
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ReportTransportError("中心兼容握手响应不是有效 JSON") from error
        if not isinstance(value, Mapping):
            raise ReportTransportError("中心兼容握手响应不是对象")
        return value

    def _signed_headers(self, path: str, body: dict[str, object]) -> dict[str, str]:
        timestamp = int(time.time())
        nonce = secrets.token_urlsafe(18)
        request = HostIdentityRequest(
            method="POST",
            path=path,
            host_id=self._host_id,
            timestamp=timestamp,
            nonce=nonce,
            body=body,
        )
        return {
            "X-Inference-Host-ID": self._host_id,
            "X-Inference-Host-Timestamp": str(timestamp),
            "X-Inference-Host-Nonce": nonce,
            "X-Inference-Host-Signature": sign_host_identity_request(
                request, private_key=self._host_private_key
            ),
        }


class _HttpResponse(Protocol):
    status: int

    def read(self) -> bytes: ...

    def close(self) -> None: ...


__all__ = ["HttpDecisionReportTransport", "ReportTransportError"]
