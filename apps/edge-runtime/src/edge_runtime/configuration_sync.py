"""按主机拉取配置并原子确认本地视图。"""

from __future__ import annotations

import json
import secrets
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol, cast

from nvsop_contracts import (
    ConfigurationBundle,
    HostIdentityRequest,
    configuration_from_wire,
    sign_host_identity_request,
)

from edge_runtime.local_state.configuration import ConfigurationFailure, LocalConfigurationStore


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
    applied: bool
    active: ConfigurationBundle | None
    failure: ConfigurationFailure | None


class HttpConfigurationPuller(ConfigurationPuller):
    """为一台指定推理机发送带签名的 GET 请求。"""

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
            raise ValueError("configuration pull identity and URL must not be empty")
        if timeout <= 0:
            raise ValueError("configuration pull timeout must be positive")
        self._base_url = center_url.rstrip("/")
        self._host_id = host_id
        self._host_private_key = host_private_key
        self._timeout = timeout
        self._ssl_context = ssl_context

    def pull(self) -> ConfigurationBundle:
        path = f"/api/v1/inference-hosts/{self._host_id}/configuration"
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            headers={
                "Accept": "application/json",
                **self._signed_headers(path),
            },
            method="GET",
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
            raise ConfigurationPullError(
                "http_rejected", "中心配置接口拒绝请求", status=status
            ) from error
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            raise ConfigurationPullError("center_unreachable", "中心配置接口暂时不可达") from error
        try:
            if response.status != 200:
                raise ConfigurationPullError(
                    "http_rejected", "中心配置接口返回非成功状态", status=response.status
                )
            try:
                raw = response.read()
            except (OSError, TimeoutError) as error:
                raise ConfigurationPullError(
                    "center_unreachable", "中心配置接口暂时不可达"
                ) from error
        finally:
            with suppress(OSError):
                response.close()
        try:
            value = json.loads(raw.decode("utf-8"))
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

    def _signed_headers(self, path: str) -> dict[str, str]:
        timestamp = int(time.time())
        nonce = secrets.token_urlsafe(18)
        request = HostIdentityRequest(
            method="GET",
            path=path,
            host_id=self._host_id,
            timestamp=timestamp,
            nonce=nonce,
            body=None,
        )
        return {
            "X-Inference-Host-ID": self._host_id,
            "X-Inference-Host-Timestamp": str(timestamp),
            "X-Inference-Host-Nonce": nonce,
            "X-Inference-Host-Signature": sign_host_identity_request(
                request, private_key=self._host_private_key
            ),
        }


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
        try:
            bundle = self._puller.pull()
            if self._expected_host_id is not None and bundle.host_id != self._expected_host_id:
                raise ConfigurationPullError("host_scope_mismatch", "配置响应不属于本机推理机")
            if self._validator is not None:
                self._validator(bundle)
            self._store.confirm(bundle, confirmed_at=observed_at)
        except ConfigurationPullError as error:
            self._store.record_failure(
                code=error.code,
                detail=str(error),
                observed_at=observed_at,
            )
        except OSError:
            self._store.record_failure(
                code="center_unreachable",
                detail="中心配置接口暂时不可达",
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
            elif "revision is older" in detail or "time is older" in detail:
                code = "stale_revision"
            elif self._validator is not None:
                code = "runtime_configuration_invalid"
            else:
                code = "contract_invalid"
            self._store.record_failure(code=code, detail=detail, observed_at=observed_at)
        failure = self._store.failure()
        return ConfigurationSyncResult(
            applied=failure is None,
            active=self._store.confirmed(),
            failure=failure,
        )


class _HttpResponse(Protocol):
    status: int

    def read(self) -> bytes: ...

    def close(self) -> None: ...


__all__ = [
    "ConfigurationPullError",
    "ConfigurationPuller",
    "ConfigurationSyncResult",
    "ConfigurationSynchronizer",
    "HttpConfigurationPuller",
]
