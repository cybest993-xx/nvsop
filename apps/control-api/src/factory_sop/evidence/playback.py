"""具体证据的短期上下文与逐次读取授权；中心不读取媒体字节。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.evidence.model import EvidenceReference, EvidenceStatus
from factory_sop.evidence.repository import EvidenceRepository
from factory_sop.evidence.usecases import is_controlled_reference


class EvidenceAccessRefusedError(ValueError):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def evidence_for_instance(
    caller: Caller, *, evidence: EvidenceRepository, station_id: str, instance_id: int
) -> tuple[EvidenceReference, ...]:
    authorize(caller, Permission.EVIDENCE_VIEW)
    rows, _ = evidence.page(page=1, page_size=100, station_id=station_id, instance_id=instance_id)
    return rows


def issue_access_context(
    caller: Caller,
    *,
    evidence_id: str,
    fingerprint: str,
    evidence: EvidenceRepository,
    registered_hosts: tuple[UUID, ...],
    origins: Mapping[str, str],
    secret: str,
    now: float,
) -> dict[str, object]:
    authorize(caller, Permission.EVIDENCE_VIEW)
    registration = _available(evidence, evidence_id).registration
    if UUID(registration.host_id) not in registered_hosts:
        raise EvidenceAccessRefusedError(403, "source_host_unregistered")
    origin = origins.get(registration.host_id)
    if origin is None:
        raise EvidenceAccessRefusedError(503, "source_host_unconfigured")
    expires = int(now) + 120
    payload = json.dumps([evidence_id, registration.host_id, fingerprint, expires]).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(secret.encode(), b"evidence\0" + encoded.encode(), hashlib.sha256)
    return {
        "context": encoded + "." + signature.hexdigest(),
        "origin": origin,
        "expires_at": expires,
        "host_id": registration.host_id,
    }


def check_access_context(
    *,
    context: str,
    evidence_id: str,
    host_id: str,
    evidence: EvidenceRepository,
    resolve_caller: Callable[[str], Caller | None],
    secret: str,
    now: float,
) -> dict[str, object]:
    try:
        if len(context) > 512:
            raise ValueError
        encoded, signature = context.split(".")
        expected = hmac.new(secret.encode(), b"evidence\0" + encoded.encode(), hashlib.sha256)
        if not hmac.compare_digest(signature, expected.hexdigest()):
            raise ValueError
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if (
            not isinstance(claims, list)
            or len(claims) != 4
            or not all(isinstance(value, str) and value for value in claims[:3])
            or type(claims[3]) is not int
            or claims[0] != evidence_id
            or claims[1] != host_id
        ):
            raise ValueError
    except (ValueError, TypeError, UnicodeError) as error:
        raise EvidenceAccessRefusedError(403, "access_denied") from error
    if now >= claims[3]:
        raise EvidenceAccessRefusedError(410, "context_expired")
    caller = resolve_caller(claims[2])
    if caller is None:
        raise EvidenceAccessRefusedError(403, "session_invalid")
    authorize(caller, Permission.EVIDENCE_VIEW)
    registration = _available(evidence, evidence_id).registration
    if registration.host_id != host_id:
        raise EvidenceAccessRefusedError(403, "source_host_mismatch")
    return {
        "reference": registration.reference,
        "size": registration.size,
        "sha256": registration.sha256,
        "kind": registration.kind.value,
    }


def _available(evidence: EvidenceRepository, evidence_id: str) -> EvidenceReference:
    value = evidence.find(evidence_id)
    if value is None:
        raise EvidenceAccessRefusedError(404, "evidence_unregistered")
    if value.status is not EvidenceStatus.AVAILABLE:
        raise EvidenceAccessRefusedError(410, "reference_unavailable")
    if not is_controlled_reference(value.registration.reference or ""):
        raise EvidenceAccessRefusedError(410, "reference_expired")
    return value
