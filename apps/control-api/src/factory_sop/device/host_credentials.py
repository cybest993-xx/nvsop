"""推理机控制面凭据的签发、指纹和校验。"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

_TOKEN_BYTES = 32


@dataclass(frozen=True, slots=True)
class IssuedInferenceHostCredential:
    """一次凭据轮换产生的明文值与中心存储指纹。"""

    value: str
    fingerprint: str

    @classmethod
    def issue(cls) -> IssuedInferenceHostCredential:
        """签发高熵凭据；明文只由轮换接口返回一次。"""
        value = secrets.token_urlsafe(_TOKEN_BYTES)
        return cls(value=value, fingerprint=fingerprint(credential=value))


def fingerprint(*, credential: str) -> str:
    """计算推理机凭据的固定长度指纹，不在中心保存明文。"""
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


def matches(*, credential: str | None, stored_fingerprint: str) -> bool:
    """以常量时间比较呈现的凭据与中心指纹。"""
    if not credential or not stored_fingerprint:
        return False
    return hmac.compare_digest(fingerprint(credential=credential), stored_fingerprint)
