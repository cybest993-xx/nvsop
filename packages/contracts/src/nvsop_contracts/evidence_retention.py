"""证据删除资格：中心→来源机的窄机器契约（#206）。

中心只回答"这条已定稿证据现在是否可以删除"，以及登记/校验/结案一条删除认领；中心不删除媒体
字节、不返回本机路径，也不返回媒体内容（ADR-0012）。三个动作各有自己的严格请求体：

1. ``claim``：给定来源机自己已确认的 ``(config_revision, effective_sha256)`` 与一批本机已知的
   引用身份，返回逐条 ``eligible/protected/unknown`` 判定、原因码与短期票据；
2. ``verify``：删除前用票据重新校验同一条引用的判定是否仍然成立；
3. ``result``：把这条认领终局反馈为 ``deleted/retained/cancelled``。

字段集严格：未知字段、缺失字段、越界批次一律拒绝，身份与来源配置必须显式绑定，使来源机不能用
旧 revision 或别的主机的资格换取更宽松的判定（machine-contract-evolution.md）。
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypeVar

# 一次 claim 的候选上限：资格请求是有界批处理，不是整盘盘点。
MAX_DELETION_CANDIDATES = 64
MAX_DELETION_TICKET_CHARS = 4096
MAX_DELETION_REASON_CHARS = 64
MAX_DELETION_DETAIL_CHARS = 255
MAX_EVIDENCE_ID_CHARS = 255
MAX_REFERENCE_CHARS = 512
MAX_GENERATION_CHARS = 128

_REASON_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
_HEX_DIGITS = frozenset("0123456789abcdef")
_CATEGORIES = frozenset({"pass", "fail", "indeterminate"})
EnumT = TypeVar("EnumT", bound=StrEnum)


class EvidenceDeletionDecision(StrEnum):
    """一条引用的删除资格；只有 ``eligible`` 允许来源机删除该媒体。"""

    ELIGIBLE = "eligible"
    PROTECTED = "protected"
    UNKNOWN = "unknown"


class EvidenceDeletionOutcome(StrEnum):
    """来源机对一条已认领引用的终局反馈；``cancelled`` 表示放弃删除并释放认领。"""

    DELETED = "deleted"
    RETAINED = "retained"
    CANCELLED = "cancelled"


class EvidenceDeletionClaimStatus(StrEnum):
    """认领的持久状态；未结案的 ``pending`` 认领继续保护该引用。"""

    PENDING = "pending"
    DELETED = "deleted"
    RETAINED = "retained"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class EvidenceDeletionCandidate:
    """来源机声称的一条引用身份与媒体身份；中心用登记事实交叉校验，不直接相信本机声明。"""

    evidence_id: str
    generation: str
    sha256: str
    size: int
    reference: str

    def to_wire(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "generation": self.generation,
            "sha256": self.sha256,
            "size": self.size,
            "reference": self.reference,
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> EvidenceDeletionCandidate:
        if set(value) != {"evidence_id", "generation", "sha256", "size", "reference"}:
            raise ValueError("invalid deletion candidate fields")
        return cls(
            evidence_id=_bounded_text(
                value["evidence_id"], "deletion candidate evidence id", MAX_EVIDENCE_ID_CHARS
            ),
            generation=_bounded_text(
                value["generation"], "deletion candidate generation", MAX_GENERATION_CHARS
            ),
            sha256=_sha256(value["sha256"], "deletion candidate digest"),
            size=_count(value["size"], "deletion candidate size"),
            reference=_bounded_text(
                value["reference"], "deletion candidate reference", MAX_REFERENCE_CHARS
            ),
        )


@dataclass(frozen=True, slots=True)
class EvidenceDeletionRequest:
    """一次 claim 请求：来源机身份、来源机已确认配置身份与有界候选批次。"""

    host_id: str
    config_revision: int
    effective_sha256: str
    candidates: tuple[EvidenceDeletionCandidate, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "host_id": self.host_id,
            "config_revision": self.config_revision,
            "effective_sha256": self.effective_sha256,
            "candidates": [candidate.to_wire() for candidate in self.candidates],
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> EvidenceDeletionRequest:
        if set(value) != {"host_id", "config_revision", "effective_sha256", "candidates"}:
            raise ValueError("invalid deletion request fields")
        candidates = _candidates(value["candidates"])
        if not candidates:
            raise ValueError("deletion request must carry at least one candidate")
        if len(candidates) > MAX_DELETION_CANDIDATES:
            raise ValueError("deletion request batch exceeds the candidate limit")
        identifiers = [candidate.evidence_id for candidate in candidates]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("deletion request repeats an evidence id")
        return cls(
            host_id=_uuid_text(value["host_id"], "deletion request host id"),
            config_revision=_positive_count(
                value["config_revision"], "deletion request config revision"
            ),
            effective_sha256=_sha256(value["effective_sha256"], "deletion request digest"),
            candidates=candidates,
        )


@dataclass(frozen=True, slots=True)
class EvidenceDeletionEntry:
    """一条引用的判定：决定、机器原因码与该判定的依据；未知与受保护都必须显式可解释。"""

    evidence_id: str
    station_id: str | None
    instance_id: int | None
    generation: str | None
    decision: EvidenceDeletionDecision
    reason: str
    category: str | None
    ticket: str | None
    claim_id: str | None
    expires_at: datetime | None
    received_at: datetime | None
    age_seconds: float | None
    threshold_seconds: int | None

    def to_wire(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "station_id": self.station_id,
            "instance_id": self.instance_id,
            "generation": self.generation,
            "decision": self.decision.value,
            "reason": self.reason,
            "category": self.category,
            "ticket": self.ticket,
            "claim_id": self.claim_id,
            "expires_at": _moment(self.expires_at),
            "received_at": _moment(self.received_at),
            "age_seconds": self.age_seconds,
            "threshold_seconds": self.threshold_seconds,
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> EvidenceDeletionEntry:
        if set(value) != {
            "evidence_id",
            "station_id",
            "instance_id",
            "generation",
            "decision",
            "reason",
            "category",
            "ticket",
            "claim_id",
            "expires_at",
            "received_at",
            "age_seconds",
            "threshold_seconds",
        }:
            raise ValueError("invalid deletion entry fields")
        decision = _member(EvidenceDeletionDecision, value["decision"], "deletion decision")
        ticket = _optional_text(value["ticket"], "deletion entry ticket", MAX_DELETION_TICKET_CHARS)
        claim_id = _optional_uuid_text(value["claim_id"], "deletion entry claim id")
        category = _optional_text(value["category"], "deletion entry category", 32)
        if category is not None and category not in _CATEGORIES:
            raise ValueError("invalid deletion entry category")
        # 没有票据与认领身份就不允许声称可删除；非 eligible 也不允许携带票据。
        if decision is EvidenceDeletionDecision.ELIGIBLE and (ticket is None or claim_id is None):
            raise ValueError("eligible deletion entry must carry a ticket and a claim id")
        if decision is not EvidenceDeletionDecision.ELIGIBLE and (
            ticket is not None or claim_id is not None
        ):
            raise ValueError("non-eligible deletion entry must not carry a ticket")
        return cls(
            evidence_id=_bounded_text(
                value["evidence_id"], "deletion entry evidence id", MAX_EVIDENCE_ID_CHARS
            ),
            station_id=_optional_uuid_text(value["station_id"], "deletion entry station id"),
            instance_id=_optional_count(value["instance_id"], "deletion entry instance id"),
            generation=_optional_text(
                value["generation"], "deletion entry generation", MAX_GENERATION_CHARS
            ),
            decision=decision,
            reason=_reason(value["reason"], "deletion entry reason"),
            category=category,
            ticket=ticket,
            claim_id=claim_id,
            expires_at=_optional_moment(value["expires_at"], "deletion entry expiry"),
            received_at=_optional_moment(value["received_at"], "deletion entry received time"),
            age_seconds=_optional_seconds(value["age_seconds"], "deletion entry age"),
            threshold_seconds=_optional_count(
                value["threshold_seconds"], "deletion entry threshold"
            ),
        )


@dataclass(frozen=True, slots=True)
class EvidenceDeletionResponse:
    """一次 claim 的结果：绑定来源配置身份、票据有效期与逐条判定。"""

    host_id: str
    config_revision: int
    effective_sha256: str
    ticket_ttl_seconds: int | None
    entries: tuple[EvidenceDeletionEntry, ...]

    def to_wire(self) -> dict[str, object]:
        return {
            "host_id": self.host_id,
            "config_revision": self.config_revision,
            "effective_sha256": self.effective_sha256,
            "ticket_ttl_seconds": self.ticket_ttl_seconds,
            "entries": [entry.to_wire() for entry in self.entries],
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> EvidenceDeletionResponse:
        if set(value) != {
            "host_id",
            "config_revision",
            "effective_sha256",
            "ticket_ttl_seconds",
            "entries",
        }:
            raise ValueError("invalid deletion response fields")
        ttl = _optional_positive_count(value["ticket_ttl_seconds"], "deletion ticket lifetime")
        entries = value["entries"]
        if not isinstance(entries, list) or not all(isinstance(item, Mapping) for item in entries):
            raise ValueError("invalid deletion response entries")
        parsed = tuple(EvidenceDeletionEntry.from_wire(item) for item in entries)
        for entry in parsed:
            if entry.decision is EvidenceDeletionDecision.ELIGIBLE and ttl is None:
                raise ValueError("eligible deletion entry without a configured ticket lifetime")
        return cls(
            host_id=_uuid_text(value["host_id"], "deletion response host id"),
            config_revision=_positive_count(
                value["config_revision"], "deletion response config revision"
            ),
            effective_sha256=_sha256(value["effective_sha256"], "deletion response digest"),
            ticket_ttl_seconds=ttl,
            entries=parsed,
        )


@dataclass(frozen=True, slots=True)
class EvidenceDeletionVerifyRequest:
    """删除前校验：票据 + 来源机当前已确认配置 + 本机重新读到的同一条媒体身份。

    票据本身就是认领指针（已签名），因此请求不再单独携带认领 ID。
    """

    host_id: str
    ticket: str
    config_revision: int
    effective_sha256: str
    candidate: EvidenceDeletionCandidate

    def to_wire(self) -> dict[str, object]:
        return {
            "host_id": self.host_id,
            "ticket": self.ticket,
            "config_revision": self.config_revision,
            "effective_sha256": self.effective_sha256,
        } | self.candidate.to_wire()

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> EvidenceDeletionVerifyRequest:
        if set(value) != {
            "host_id",
            "ticket",
            "config_revision",
            "effective_sha256",
            "evidence_id",
            "generation",
            "sha256",
            "size",
            "reference",
        }:
            raise ValueError("invalid deletion verify fields")
        candidate = EvidenceDeletionCandidate.from_wire(
            {
                key: value[key]
                for key in ("evidence_id", "generation", "sha256", "size", "reference")
            }
        )
        return cls(
            host_id=_uuid_text(value["host_id"], "deletion verify host id"),
            ticket=_bounded_text(
                value["ticket"], "deletion verify ticket", MAX_DELETION_TICKET_CHARS
            ),
            config_revision=_positive_count(
                value["config_revision"], "deletion verify config revision"
            ),
            effective_sha256=_sha256(value["effective_sha256"], "deletion verify digest"),
            candidate=candidate,
        )


@dataclass(frozen=True, slots=True)
class EvidenceDeletionVerifyResponse:
    """删除前校验结果；只有 ``eligible`` 允许来源机继续删除该文件。"""

    claim_id: str
    evidence_id: str
    decision: EvidenceDeletionDecision
    reason: str
    expires_at: datetime | None

    def to_wire(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "evidence_id": self.evidence_id,
            "decision": self.decision.value,
            "reason": self.reason,
            "expires_at": _moment(self.expires_at),
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> EvidenceDeletionVerifyResponse:
        if set(value) != {"claim_id", "evidence_id", "decision", "reason", "expires_at"}:
            raise ValueError("invalid deletion verify response fields")
        return cls(
            claim_id=_uuid_text(value["claim_id"], "deletion verify response claim id"),
            evidence_id=_bounded_text(
                value["evidence_id"], "deletion verify response evidence id", MAX_EVIDENCE_ID_CHARS
            ),
            decision=_member(
                EvidenceDeletionDecision, value["decision"], "deletion verify decision"
            ),
            reason=_reason(value["reason"], "deletion verify response reason"),
            expires_at=_optional_moment(value["expires_at"], "deletion verify response expiry"),
        )


@dataclass(frozen=True, slots=True)
class EvidenceDeletionResultRequest:
    """来源机对一条认领的终局反馈；``reason_code`` 必须解释本机为何这样结案。"""

    host_id: str
    claim_id: str
    evidence_id: str
    outcome: EvidenceDeletionOutcome
    reason_code: str
    detail: str | None = None

    def to_wire(self) -> dict[str, object]:
        return {
            "host_id": self.host_id,
            "claim_id": self.claim_id,
            "evidence_id": self.evidence_id,
            "outcome": self.outcome.value,
            "reason_code": self.reason_code,
            "detail": self.detail,
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> EvidenceDeletionResultRequest:
        if set(value) != {"host_id", "claim_id", "evidence_id", "outcome", "reason_code", "detail"}:
            raise ValueError("invalid deletion result fields")
        return cls(
            host_id=_uuid_text(value["host_id"], "deletion result host id"),
            claim_id=_uuid_text(value["claim_id"], "deletion result claim id"),
            evidence_id=_bounded_text(
                value["evidence_id"], "deletion result evidence id", MAX_EVIDENCE_ID_CHARS
            ),
            outcome=_member(EvidenceDeletionOutcome, value["outcome"], "deletion result outcome"),
            reason_code=_reason(value["reason_code"], "deletion result reason code"),
            detail=_optional_text(
                value["detail"], "deletion result detail", MAX_DELETION_DETAIL_CHARS
            ),
        )


@dataclass(frozen=True, slots=True)
class EvidenceDeletionResultResponse:
    """终局反馈后的认领状态；重复反馈同一结果按同样的状态返回。"""

    claim_id: str
    evidence_id: str
    status: EvidenceDeletionClaimStatus
    reason_code: str

    def to_wire(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "evidence_id": self.evidence_id,
            "status": self.status.value,
            "reason_code": self.reason_code,
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> EvidenceDeletionResultResponse:
        if set(value) != {"claim_id", "evidence_id", "status", "reason_code"}:
            raise ValueError("invalid deletion result response fields")
        return cls(
            claim_id=_uuid_text(value["claim_id"], "deletion result response claim id"),
            evidence_id=_bounded_text(
                value["evidence_id"], "deletion result response evidence id", MAX_EVIDENCE_ID_CHARS
            ),
            status=_member(
                EvidenceDeletionClaimStatus, value["status"], "deletion result response status"
            ),
            reason_code=_reason(value["reason_code"], "deletion result response reason code"),
        )


def _candidates(value: object) -> tuple[EvidenceDeletionCandidate, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise ValueError("invalid deletion request candidates")
    candidates: list[EvidenceDeletionCandidate] = []
    for item in value:
        # 候选元素必须先是对象; null/整数/字符串在此拒绝为 422, 不能把 TypeError 漏到 500。
        if not isinstance(item, Mapping):
            raise ValueError("invalid deletion candidate")
        candidates.append(EvidenceDeletionCandidate.from_wire(item))
    return tuple(candidates)


def _moment(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _optional_moment(value: object, label: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"invalid {label}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"invalid {label}") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must carry a UTC offset")
    return parsed.astimezone(UTC)


def _reason(value: object, label: str) -> str:
    if not isinstance(value, str) or _REASON_PATTERN.fullmatch(value) is None:
        raise ValueError(f"invalid {label}")
    return value


def _member(enum: type[EnumT], value: object, label: str) -> EnumT:
    if not isinstance(value, str):
        raise ValueError(f"invalid {label}")
    try:
        return enum(value)
    except ValueError as error:
        raise ValueError(f"invalid {label}") from error


def _bounded_text(value: object, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f"invalid {label}")
    if any(ord(character) < 0x20 for character in value):
        raise ValueError(f"invalid {label}")
    return value


def _optional_text(value: object, label: str, limit: int) -> str | None:
    return None if value is None else _bounded_text(value, label, limit)


def _uuid_text(value: object, label: str) -> str:
    text = _bounded_text(value, label, 36)
    try:
        parsed = uuid.UUID(text)
    except ValueError as error:
        raise ValueError(f"invalid {label}") from error
    return str(parsed)


def _optional_uuid_text(value: object, label: str) -> str | None:
    return None if value is None else _uuid_text(value, label)


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid {label}")
    lowered = value.lower()
    if len(lowered) != 64 or any(character not in _HEX_DIGITS for character in lowered):
        raise ValueError(f"invalid {label}")
    return lowered


def _count(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"invalid {label}")
    return value


def _positive_count(value: object, label: str) -> int:
    parsed = _count(value, label)
    if parsed < 1:
        raise ValueError(f"invalid {label}")
    return parsed


def _optional_count(value: object, label: str) -> int | None:
    return None if value is None else _count(value, label)


def _optional_positive_count(value: object, label: str) -> int | None:
    return None if value is None else _positive_count(value, label)


def _optional_seconds(value: object, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"invalid {label}")
    if not math.isfinite(float(value)) or float(value) < 0:
        raise ValueError(f"invalid {label}")
    return float(value)


__all__ = [
    "MAX_DELETION_CANDIDATES",
    "MAX_DELETION_DETAIL_CHARS",
    "MAX_DELETION_REASON_CHARS",
    "MAX_DELETION_TICKET_CHARS",
    "EvidenceDeletionCandidate",
    "EvidenceDeletionClaimStatus",
    "EvidenceDeletionDecision",
    "EvidenceDeletionEntry",
    "EvidenceDeletionOutcome",
    "EvidenceDeletionRequest",
    "EvidenceDeletionResponse",
    "EvidenceDeletionResultRequest",
    "EvidenceDeletionResultResponse",
    "EvidenceDeletionVerifyRequest",
    "EvidenceDeletionVerifyResponse",
]
