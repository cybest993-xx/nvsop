"""evidence 用例与适配器共享的稳定拒绝错误。"""

from __future__ import annotations

from enum import StrEnum


class EvidenceRefusal(StrEnum):
    """登记被拒绝的原因；与 ``EvidenceStatus.FAILED`` 的 ``failure_reason`` 同源。"""

    HOST_NOT_OWNER = "host_not_owner"
    ILLEGAL_REFERENCE = "illegal_reference"
    IDENTITY_CONFLICT = "identity_conflict"
    # 该证据存在未结案删除认领：引用写入必须先解决认领，不能与删除并行产生新引用。
    DELETION_CLAIM_PENDING = "deletion_claim_pending"
    # 该证据媒体由来源机确认已删除：拒绝把它当作新引用重新登记（§5.19 不可复活）。
    MEDIA_DELETED = "media_deleted"


class EvidenceRefusedError(ValueError):
    """该登记不能作为中心证据引用接受；被拒绝的请求不产生任何持久化事实。"""

    def __init__(self, code: EvidenceRefusal, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


__all__ = ["EvidenceRefusal", "EvidenceRefusedError"]
