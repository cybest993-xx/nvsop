"""保留影响确认令牌：绑定基准版本、目标策略、参考时刻与真实计数（HTTP 适配层）。

令牌是不透明的：确认时原样带回，服务端用它记录的参考时刻重算同一份估算并比对摘要。
基准、目标或计数任一变化都会改变摘要，使旧确认不能用于盲签。
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime

from factory_sop.retention.api import RetentionImpactEstimate, impact_digest


@dataclass(frozen=True, slots=True)
class EstimateToken:
    """一次估算的确认凭据：参考时刻用于确定性重算，摘要用于比对。"""

    reference_time: datetime
    digest: str


def encode_estimate_token(estimate: RetentionImpactEstimate) -> str:
    """把估算编码成不透明令牌。"""
    payload = json.dumps(
        {"at": estimate.reference_time.isoformat(), "digest": impact_digest(estimate)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_estimate_token(token: str | None) -> EstimateToken | None:
    """解析确认令牌；缺失、格式非法或时区缺失时返回 ``None``（调用方按“必须先估算”拒绝）。"""
    if token is None:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(token.encode()))
    except (binascii.Error, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    reference = payload.get("at")
    digest = payload.get("digest")
    if not isinstance(reference, str) or not isinstance(digest, str):
        return None
    try:
        parsed = datetime.fromisoformat(reference)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return EstimateToken(reference_time=parsed, digest=digest)


__all__ = ["EstimateToken", "decode_estimate_token", "encode_estimate_token"]
