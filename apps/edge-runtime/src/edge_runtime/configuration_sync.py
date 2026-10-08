"""按主机拉取配置并原子确认本地视图。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from nvsop_contracts import (
    DISPOSITION_STOP_OUTPUT_CAPABILITY,
    EXECUTION_LEASE_WRITE_GATE_CAPABILITY,
    ConfigurationBundle,
    configuration_from_wire,
)

from edge_runtime.center_client import CenterClient, CenterUnreachableError
from edge_runtime.local_state.configuration import ConfigurationFailure, LocalConfigurationStore

_SUPPORTED_CONFIGURATION_CAPABILITIES: frozenset[str] = frozenset(
    {DISPOSITION_STOP_OUTPUT_CAPABILITY, EXECUTION_LEASE_WRITE_GATE_CAPABILITY}
)


class ConfigurationPullError(RuntimeError):
    """拉取在替换本地确认状态前失败。"""

    def __init__(self, code: str, detail: str, *, status: int | None = None) -> None:
        super().__init__(detail)
        self.code = code
        self.status = status


class ConfigurationPuller(Protocol):
    def pull(self) -> ConfigurationBundle: ...


@dataclass(frozen=True, slots=True)
class ConfigurationSyncResult:
    candidate: ConfigurationBundle | None
    confirmed: ConfigurationBundle | None
    failure: ConfigurationFailure | None


class HttpConfigurationPuller(ConfigurationPuller):
    """为一台指定推理机发送带签名的 GET 请求。"""

    def __init__(self, *, client: CenterClient, host_id: str) -> None:
        self._client = client
        self._host_id = host_id

    def pull(self) -> ConfigurationBundle:
        try:
            response = self._client.get(f"/api/v1/inference-hosts/{self._host_id}/configuration")
        except CenterUnreachableError as error:
            raise ConfigurationPullError("center_unreachable", "中心配置接口暂时不可达") from error
        if response.status != 200:
            raise ConfigurationPullError(
                "http_rejected", "中心配置接口返回非成功状态", status=response.status
            )
        try:
            value = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ConfigurationPullError("invalid_json", "中心配置响应不是有效 JSON") from error
        if not isinstance(value, Mapping):
            raise ConfigurationPullError("invalid_shape", "中心配置响应不是对象")
        try:
            bundle = configuration_from_wire(value)
        except ValueError as error:
            detail = str(error)
            code = "digest_mismatch" if "digest" in detail else "contract_invalid"
            raise ConfigurationPullError(code, detail) from error
        if bundle.host_id != self._host_id:
            raise ConfigurationPullError("host_scope_mismatch", "配置响应不属于发起请求的推理机")
        return bundle


class ConfigurationSynchronizer:
    """新拉取无效或不可达时,继续使用最后确认的 bundle。"""

    def __init__(
        self,
        *,
        puller: ConfigurationPuller,
        store: LocalConfigurationStore,
        expected_host_id: str | None = None,
        validator: Callable[[ConfigurationBundle], None] | None = None,
    ) -> None:
        self._puller = puller
        self._store = store
        self._expected_host_id = expected_host_id
        self._validator = validator

    def synchronize(self, *, observed_at: float) -> ConfigurationSyncResult:
        """拉取并校验候选,但不把候选写成 durable confirmed。"""
        try:
            bundle = self._puller.pull()
            if self._expected_host_id is not None and bundle.host_id != self._expected_host_id:
                raise ConfigurationPullError("host_scope_mismatch", "配置响应不属于本机推理机")
            unsupported = tuple(
                capability
                for capability in bundle.required_capabilities
                if capability not in _SUPPORTED_CONFIGURATION_CAPABILITIES
            )
            if unsupported:
                raise ConfigurationPullError(
                    "unsupported_capability",
                    f"推理机不支持配置能力要求: {', '.join(unsupported)}",
                )
            self._store.validate_candidate(bundle)
        except ConfigurationPullError as error:
            return self._failed(error.code, str(error), observed_at=observed_at)
        except OSError:
            return self._failed(
                "center_unreachable",
                "中心配置接口暂时不可达",
                observed_at=observed_at,
            )
        except (TypeError, ValueError) as error:
            detail = str(error)
            if "host scope" in detail:
                code = "host_scope_mismatch"
            elif "digest" in detail:
                code = "digest_mismatch"
            elif "reused with different content" in detail:
                code = "conflicting_confirmation"
            elif "revision is older" in detail:
                code = "stale_revision"
            else:
                code = "contract_invalid"
            return self._failed(code, detail, observed_at=observed_at)

        if self._validator is not None:
            try:
                self._validator(bundle)
            except (TypeError, ValueError) as error:
                return self._failed(
                    "runtime_configuration_invalid",
                    str(error),
                    observed_at=observed_at,
                )
        return ConfigurationSyncResult(
            candidate=bundle,
            confirmed=self._store.confirmed(),
            failure=None,
        )

    def confirm(self, bundle: ConfigurationBundle, *, confirmed_at: float) -> None:
        """仅在运行时完成实际切换后持久确认同一候选。"""
        self._store.confirm(bundle, confirmed_at=confirmed_at)

    def reject_application(self, *, detail: str, observed_at: float) -> None:
        """记录候选已验证但运行时组合/切换失败。"""
        self._store.record_failure(
            code="runtime_application_failed",
            detail=detail,
            observed_at=observed_at,
        )

    def _failed(self, code: str, detail: str, *, observed_at: float) -> ConfigurationSyncResult:
        self._store.record_failure(code=code, detail=detail, observed_at=observed_at)
        return ConfigurationSyncResult(
            candidate=None,
            confirmed=self._store.confirmed(),
            failure=self._store.failure(),
        )


__all__ = [
    "ConfigurationPullError",
    "ConfigurationPuller",
    "ConfigurationSyncResult",
    "ConfigurationSynchronizer",
    "HttpConfigurationPuller",
]
