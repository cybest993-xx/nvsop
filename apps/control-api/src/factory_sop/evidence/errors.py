"""evidence 用例与适配器共享的稳定拒绝错误。"""

from __future__ import annotations

from enum import StrEnum


class EvidenceRefusal(StrEnum):
    """登记被拒绝的原因；与 ``EvidenceStatus.FAILED`` 的 ``failure_reason`` 同源。"""

    HOST_NOT_OWNER = "host_not_owner"
    ILLEGAL_REFERENCE = "illegal_reference"
    IDENTITY_CONFLICT = "identity_conflict"


class EvidenceRefusedError(ValueError):
    """该登记不能作为中心证据引用接受；被拒绝的请求不产生任何持久化事实。"""

    def __init__(self, code: EvidenceRefusal, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


__all__ = ["EvidenceRefusal", "EvidenceRefusedError"]
