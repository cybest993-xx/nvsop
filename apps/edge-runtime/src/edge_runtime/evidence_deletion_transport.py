"""中心证据删除资格接口的主机签名 HTTP 适配器 (#206)。

claim / verify / result 三个动作都只认登记主机签名; 本适配器只承载契约编解码与网络错误分类,
不读本机状态也不决定删除。中心返回非成功状态时抛出可解释原因, 由执行器跳过并保留认领。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import TypeVar

from nvsop_contracts import (
    EvidenceDeletionRequest,
    EvidenceDeletionResponse,
    EvidenceDeletionResultRequest,
    EvidenceDeletionResultResponse,
    EvidenceDeletionVerifyRequest,
    EvidenceDeletionVerifyResponse,
)

from edge_runtime.center_client import CenterClient, CenterResponse, CenterUnreachableError
from edge_runtime.evidence_deletion import (
    EvidenceDeletionTransport,
    EvidenceDeletionTransportError,
)

CLAIMS_PATH = "/api/v1/retention/evidence-deletion-claims"

ResponseT = TypeVar("ResponseT")


class HttpEvidenceDeletionTransport(EvidenceDeletionTransport):
    """通过中心窄契约领取资格、删前校验并反馈终局。"""

    def __init__(self, *, client: CenterClient, host_id: str) -> None:
        if not host_id:
            raise ValueError("evidence deletion transport host id must not be empty")
        self._client = client
        self._host_id = host_id

    def claim(self, request: EvidenceDeletionRequest) -> EvidenceDeletionResponse:
        self._require_host(request.host_id)
        body = self._post(CLAIMS_PATH, request.to_wire())
        return _decode(body, EvidenceDeletionResponse.from_wire)

    def verify(self, request: EvidenceDeletionVerifyRequest) -> EvidenceDeletionVerifyResponse:
        self._require_host(request.host_id)
        body = self._post(f"{CLAIMS_PATH}/verify", request.to_wire())
        return _decode(body, EvidenceDeletionVerifyResponse.from_wire)

    def report(self, request: EvidenceDeletionResultRequest) -> EvidenceDeletionResultResponse:
        self._require_host(request.host_id)
        body = self._post(f"{CLAIMS_PATH}/result", request.to_wire())
        return _decode(body, EvidenceDeletionResultResponse.from_wire)

    def _require_host(self, host_id: str) -> None:
        if host_id != self._host_id:
            raise EvidenceDeletionTransportError("deletion request host does not match client")

    def _post(self, path: str, body: dict[str, object]) -> Mapping[str, object]:
        response = self._call(lambda: self._client.post(path, body))
        if response.status != 200:
            raise EvidenceDeletionTransportError(
                "center rejected the evidence deletion request", status=response.status
            )
        try:
            document = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EvidenceDeletionTransportError(
                "center deletion response is not valid JSON"
            ) from error
        if not isinstance(document, Mapping):
            raise EvidenceDeletionTransportError("center deletion response is not an object")
        return document

    @staticmethod
    def _call(send: Callable[[], CenterResponse]) -> CenterResponse:
        try:
            return send()
        except CenterUnreachableError as error:
            raise EvidenceDeletionTransportError(
                "center deletion interface is unreachable"
            ) from error


def _decode(
    document: Mapping[str, object], decode: Callable[[Mapping[str, object]], ResponseT]
) -> ResponseT:
    try:
        return decode(document)
    except ValueError as error:
        raise EvidenceDeletionTransportError(
            "center deletion response violates the contract"
        ) from error


__all__ = ["CLAIMS_PATH", "HttpEvidenceDeletionTransport"]
