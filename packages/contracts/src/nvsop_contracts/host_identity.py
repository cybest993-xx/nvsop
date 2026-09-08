"""推理机主机身份的标准库公钥签名契约。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from math import gcd
from typing import cast

_ALGORITHM = "rsa-sha256"
_MINIMUM_KEY_BITS = 2048
_PUBLIC_KEY_FIELDS = frozenset({"algorithm", "exponent", "modulus"})
_PRIVATE_KEY_FIELDS = _PUBLIC_KEY_FIELDS | {"private_exponent"}
_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
_SMALL_PRIMES = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)


@dataclass(frozen=True, slots=True)
class HostIdentityKeyPair:
    """一对供推理机签名、中心验签的 RSA 身份密钥。"""

    public_key: str
    private_key: str


@dataclass(frozen=True, slots=True)
class HostIdentityRequest:
    """被主机身份签名绑定的一次 HTTP 请求。"""

    method: str
    path: str
    host_id: str
    timestamp: int
    nonce: str
    body: Mapping[str, object] | None

    def __post_init__(self) -> None:
        if not self.method or not self.path.startswith("/"):
            raise ValueError("主机身份请求必须包含 HTTP 方法和绝对路径")
        if not self.host_id or self.timestamp <= 0 or not self.nonce:
            raise ValueError("主机身份请求缺少身份、时间戳或随机数")
        if len(self.nonce) > 128:
            raise ValueError("主机身份请求随机数过长")


def generate_host_identity_key_pair(*, bits: int = _MINIMUM_KEY_BITS) -> HostIdentityKeyPair:
    """生成达到最低位数要求的主机身份密钥。"""
    if bits < _MINIMUM_KEY_BITS or bits % 2:
        raise ValueError(f"主机身份密钥至少需要偶数的 {_MINIMUM_KEY_BITS} 位")
    exponent = 65537
    half_bits = bits // 2
    while True:
        first = _prime(half_bits)
        second = _prime(half_bits)
        if first == second:
            continue
        modulus = first * second
        if modulus.bit_length() < bits:
            continue
        totient = (first - 1) * (second - 1)
        if gcd(exponent, totient) != 1:
            continue
        private_exponent = pow(exponent, -1, totient)
        return HostIdentityKeyPair(
            public_key=_encode_key(
                {"algorithm": _ALGORITHM, "exponent": exponent, "modulus": _b64(modulus)}
            ),
            private_key=_encode_key(
                {
                    "algorithm": _ALGORITHM,
                    "exponent": exponent,
                    "modulus": _b64(modulus),
                    "private_exponent": _b64(private_exponent),
                }
            ),
        )


def validate_host_identity_public_key(value: str) -> None:
    """校验中心可保存的非秘密主机公钥。"""
    _parse_key(value, private=False)


def validate_host_identity_private_key(value: str) -> None:
    """校验推理机本地私钥格式，不返回或保存私钥内容。"""
    _parse_key(value, private=True)


def sign_host_identity_request(request: HostIdentityRequest, *, private_key: str) -> str:
    """用主机私钥签名一次请求，返回无填充的 URL-safe Base64。"""
    modulus, _exponent, private_exponent = _parse_key(private_key, private=True)
    assert private_exponent is not None
    width = (modulus.bit_length() + 7) // 8
    encoded = _encoded_message(_canonical_request(request), width=width)
    signature = pow(int.from_bytes(encoded, "big"), private_exponent, modulus)
    return _b64(signature, width=width)


def verify_host_identity_request(
    request: HostIdentityRequest, *, public_key: str, signature: str
) -> bool:
    """用已登记公钥验证请求，任何格式或签名错误都返回 False。"""
    try:
        modulus, exponent, _ = _parse_key(public_key, private=False)
        encoded_signature = _from_b64(signature)
        width = (modulus.bit_length() + 7) // 8
        if len(encoded_signature) != width:
            return False
        signature_number = int.from_bytes(encoded_signature, "big")
        if signature_number >= modulus:
            return False
        recovered = pow(signature_number, exponent, modulus).to_bytes(width, "big")
        return hmac.compare_digest(
            recovered, _encoded_message(_canonical_request(request), width=width)
        )
    except (TypeError, ValueError, binascii.Error):
        return False


def _canonical_request(request: HostIdentityRequest) -> bytes:
    body = "" if request.body is None else _canonical_json(request.body)
    body_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return "\n".join(
        (
            request.method.upper(),
            request.path,
            request.host_id,
            str(request.timestamp),
            request.nonce,
            body_digest,
        )
    ).encode("utf-8")


def _encoded_message(message: bytes, *, width: int) -> bytes:
    digest_info = _DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    padding = width - len(digest_info) - 3
    if padding < 8:
        raise ValueError("主机身份密钥长度不足")
    return b"\x00\x01" + b"\xff" * padding + b"\x00" + digest_info


def _parse_key(value: str, *, private: bool) -> tuple[int, int, int | None]:
    if not isinstance(value, str) or not value:
        raise ValueError("主机身份密钥不能为空")
    try:
        raw_value: object = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("主机身份密钥不是有效 JSON") from error
    raw = _object(raw_value)
    expected = _PRIVATE_KEY_FIELDS if private else _PUBLIC_KEY_FIELDS
    if set(raw) != expected:
        raise ValueError("主机身份密钥字段不完整或包含未知字段")
    if raw.get("algorithm") != _ALGORITHM:
        raise ValueError("主机身份密钥算法不受支持")
    exponent = raw.get("exponent")
    if isinstance(exponent, bool) or not isinstance(exponent, int) or exponent <= 1:
        raise ValueError("主机身份密钥指数无效")
    modulus = _from_b64_string(raw.get("modulus"), "modulus")
    modulus_number = int.from_bytes(modulus, "big")
    if modulus_number.bit_length() < _MINIMUM_KEY_BITS or modulus_number % 2 == 0:
        raise ValueError("主机身份密钥位数不足")
    if exponent >= modulus_number or exponent % 2 == 0:
        raise ValueError("主机身份密钥指数无效")
    private_number: int | None = None
    if private:
        private_value = _from_b64_string(raw.get("private_exponent"), "private_exponent")
        private_number = int.from_bytes(private_value, "big")
        if not 1 < private_number < modulus_number:
            raise ValueError("主机身份私钥指数无效")
        for base in (2, 3, 5, 17):
            if (
                gcd(base, modulus_number) == 1
                and pow(pow(base, exponent, modulus_number), private_number, modulus_number) != base
            ):
                raise ValueError("主机身份私钥与公钥不匹配")
    return modulus_number, exponent, private_number


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("主机身份密钥必须是 JSON 对象")
    return cast(dict[str, object], value)


def _from_b64_string(value: object, name: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"主机身份密钥的 {name} 无效")
    try:
        return _from_b64(value)
    except (TypeError, ValueError, binascii.Error) as error:
        raise ValueError(f"主机身份密钥的 {name} 无效") from error


def _b64(value: int, *, width: int | None = None) -> str:
    if value < 0:
        raise ValueError("不能编码负整数")
    size = width or max(1, (value.bit_length() + 7) // 8)
    return base64.urlsafe_b64encode(value.to_bytes(size, "big")).rstrip(b"=").decode("ascii")


def _from_b64(value: str) -> bytes:
    if not value:
        raise ValueError("Base64 值不能为空")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _encode_key(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_json(value: Mapping[str, object]) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValueError("主机身份请求体必须是 JSON") from error


def _prime(bits: int) -> int:
    while True:
        candidate = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if _is_probable_prime(candidate):
            return candidate


def _is_probable_prime(value: int) -> bool:
    for prime in _SMALL_PRIMES:
        if value == prime:
            return True
        if value % prime == 0:
            return False
    exponent = value - 1
    trailing_twos = 0
    while exponent % 2 == 0:
        exponent //= 2
        trailing_twos += 1
    for _ in range(40):
        base = secrets.randbelow(value - 3) + 2
        probe = pow(base, exponent, value)
        if probe in (1, value - 1):
            continue
        for _ in range(trailing_twos - 1):
            probe = pow(probe, 2, value)
            if probe == value - 1:
                break
        else:
            return False
    return True


__all__ = [
    "HostIdentityKeyPair",
    "HostIdentityRequest",
    "generate_host_identity_key_pair",
    "sign_host_identity_request",
    "validate_host_identity_private_key",
    "validate_host_identity_public_key",
    "verify_host_identity_request",
]
