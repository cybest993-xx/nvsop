"""推理机本机证据媒体的窄删除执行器 (#206 AC2)。

中心只回答"这条已定稿引用现在是否可以删除"; 实际删除只能由拥有文件的推理机执行 (ADR-0012),
且必须在删除动画之前持有与当前策略/保护事实一致的资格。本模块是那条公开删除入口: 它按本机
SQLite 已登记产物构造候选, 经主机签名 claim → verify → 本机删除 → result 走完一次执行。

**未知、陈旧、失配或中心不可核实时一律跳过并记录稳定原因, 绝不按旧 bool 盲删。** 删除动画前在
本机 SQLite 写事务临界区内重读最后确认配置与保护事实 (未结案实例、锁存违规、待登记证据、待办再
切片): 中心镜像可能滞后于本机判定, 本机事实优先。

**崩溃安全**: 领到中心认领后先把删除意图落本机账本, 取消链接与终局结果写在同一 SQLite 事务;
上报失败或进程重启后从账本重放, 不从文件缺失推断成功。调度属于 #207, 本模块只提供可被其调用的
执行接缝, 不建立通用任务/重试框架。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Protocol

from nvsop_contracts import (
    MAX_DELETION_CANDIDATES,
    EvidenceDeletionCandidate,
    EvidenceDeletionDecision,
    EvidenceDeletionOutcome,
    EvidenceDeletionRequest,
    EvidenceDeletionResponse,
    EvidenceDeletionResultRequest,
    EvidenceDeletionResultResponse,
    EvidenceDeletionVerifyRequest,
    EvidenceDeletionVerifyResponse,
)

from edge_runtime.evidence_media import (
    evidence_artifact_directory,
    evidence_media_identity,
)
from edge_runtime.local_state import EvidenceDeletionJournal, LocalState

_STAGE_CLAIMED = "claimed"
_STAGE_RESULT_PENDING = "result_pending"
_LOCAL_OUTCOME = {
    EvidenceDeletionOutcome.DELETED: "deleted",
    EvidenceDeletionOutcome.RETAINED: "skipped",
    EvidenceDeletionOutcome.CANCELLED: "cancelled",
    EvidenceDeletionOutcome.MISSING: "missing",
}


class EvidenceDeletionTransportError(RuntimeError):
    """中心删除资格接口暂时不可达或返回非契约响应; 调用方跳过并保留原因, 不盲删。"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class EvidenceDeletionTransport(Protocol):
    """调用中心 claim / verify / result 的主机签名接缝; 由 HTTP 适配器实现。"""

    def claim(self, request: EvidenceDeletionRequest) -> EvidenceDeletionResponse: ...

    def verify(self, request: EvidenceDeletionVerifyRequest) -> EvidenceDeletionVerifyResponse: ...

    def report(self, request: EvidenceDeletionResultRequest) -> EvidenceDeletionResultResponse: ...


@dataclass(frozen=True, slots=True)
class EvidenceDeletionExecution:
    """一条候选的本机执行结果; ``outcome`` 是 ``deleted``/``skipped``/``failed``。"""

    evidence_id: str
    outcome: str
    reason: str


class EvidenceDeletionExecutor:
    """按本机已确认配置与已登记产物执行删除资格; 不读当前时钟重解释历史, 不扫描录像目录。"""

    def __init__(
        self,
        *,
        state: LocalState,
        host_id: str,
        evidence_directory: Path,
        transport: EvidenceDeletionTransport,
        batch_size: int = MAX_DELETION_CANDIDATES,
        clock: Callable[[], float] = time,
    ) -> None:
        if not host_id:
            raise ValueError("evidence deletion host id must not be empty")
        if batch_size < 1 or batch_size > MAX_DELETION_CANDIDATES:
            raise ValueError("evidence deletion batch size is out of range")
        self._state = state
        self._host_id = host_id
        self._directory = evidence_directory
        self._transport = transport
        self._batch_size = batch_size
        self._clock = clock
        self._candidate_iterator: Iterator[tuple[str, int, EvidenceDeletionCandidate]] | None = None

    def drain(self, *, limit: int | None = None) -> tuple[EvidenceDeletionExecution, ...]:
        """执行一轮本机删除资格; 没有已确认配置时不产生任何删除, 也不猜中心策略。"""
        if limit is not None and limit <= 0:
            raise ValueError("deletion work limit must be positive")
        bundle = self._state.configuration().confirmed()
        if bundle is None or bundle.host_id != self._host_id:
            return (EvidenceDeletionExecution("", "skipped", "configuration_unknown"),)
        revision = bundle.config_revision
        digest = bundle.effective_sha256
        # 先推进已有账本: 终局结果重放, 未完成的认领继续 verify/删除; 它们不再进入新 claim。
        journal = self._state.deletion_journal(limit=limit)
        results: list[EvidenceDeletionExecution] = []
        for entry in journal:
            results.append(self._resume(entry, revision=revision, digest=digest))
        budget = None if limit is None else limit - len(journal)
        pending: list[tuple[str, int, EvidenceDeletionCandidate]] = []
        if budget is None:
            candidates = self._local_candidates()
        else:
            if self._candidate_iterator is None:
                self._candidate_iterator = self._local_candidates(limit=limit)
            candidates = self._candidate_iterator
        inspected = 0
        while budget is None or inspected < budget:
            item = next(candidates, None)
            if item is None:
                self._candidate_iterator = None
                break
            inspected += 1
            if not self._state.deletion_journal(evidence_id=item[2].evidence_id, limit=1):
                pending.append(item)
        for offset in range(0, len(pending), self._batch_size):
            batch = pending[offset : offset + self._batch_size]
            request = EvidenceDeletionRequest(
                host_id=self._host_id,
                config_revision=revision,
                effective_sha256=digest,
                candidates=tuple(candidate for _, _, candidate in batch),
            )
            try:
                response = self._transport.claim(request)
            except EvidenceDeletionTransportError:
                results.extend(
                    EvidenceDeletionExecution(item[2].evidence_id, "skipped", "center_unreachable")
                    for item in batch
                )
                break
            entries = {claim_entry.evidence_id: claim_entry for claim_entry in response.entries}
            for station_id, instance_id, candidate in batch:
                claim_entry = entries.get(candidate.evidence_id)
                if claim_entry is None:
                    results.append(
                        EvidenceDeletionExecution(
                            candidate.evidence_id, "skipped", "claim_entry_missing"
                        )
                    )
                    continue
                if claim_entry.decision is not EvidenceDeletionDecision.ELIGIBLE:
                    results.append(
                        EvidenceDeletionExecution(
                            candidate.evidence_id, "skipped", claim_entry.reason
                        )
                    )
                    continue
                if claim_entry.ticket is None or claim_entry.claim_id is None:
                    results.append(
                        EvidenceDeletionExecution(
                            candidate.evidence_id, "skipped", "claim_incomplete"
                        )
                    )
                    continue
                if claim_entry.expires_at is None or self._clock() >= (
                    claim_entry.expires_at.timestamp()
                ):
                    # 票据不可用(缺失/已过期)不能产生删除意图: 不落账、不删除。
                    results.append(
                        EvidenceDeletionExecution(
                            candidate.evidence_id, "skipped", "ticket_expired"
                        )
                    )
                    continue
                # 先落本机意图再 verify/删除: 中心认领已存在, 账本使重启后仍能收敛它。
                self._state.record_deletion_intent(
                    evidence_id=candidate.evidence_id,
                    station_id=station_id,
                    instance_id=instance_id,
                    claim_id=claim_entry.claim_id,
                    ticket=claim_entry.ticket,
                    claim_expires_at=None
                    if claim_entry.expires_at is None
                    else claim_entry.expires_at.timestamp(),
                    generation=candidate.generation,
                    observed_sha256=candidate.sha256,
                    observed_size=candidate.size,
                    reference=candidate.reference,
                    at=self._clock(),
                )
                intent = self._state.deletion_journal(evidence_id=candidate.evidence_id, limit=1)
                record = next(
                    (row for row in intent if row.evidence_id == candidate.evidence_id), None
                )
                if record is None:
                    results.append(
                        EvidenceDeletionExecution(
                            candidate.evidence_id, "failed", "intent_not_recorded"
                        )
                    )
                    continue
                results.append(self._execute_intent(record, revision=revision, digest=digest))
        return tuple(results)

    def _resume(
        self, entry: EvidenceDeletionJournal, *, revision: int, digest: str
    ) -> EvidenceDeletionExecution:
        """重放一条账本行: 终局结果重报中心, 未完成认领继续执行。"""
        if entry.stage == _STAGE_RESULT_PENDING:
            outcome = _outcome_of(entry.outcome)
            if outcome is None:
                self._state.clear_deletion_intent(entry.evidence_id)
                return EvidenceDeletionExecution(entry.evidence_id, "failed", "intent_corrupt")
            return self._report_and_clear(entry, outcome, entry.reason_code or "result_replayed")
        return self._execute_intent(entry, revision=revision, digest=digest)

    def _execute_intent(
        self, entry: EvidenceDeletionJournal, *, revision: int, digest: str
    ) -> EvidenceDeletionExecution:
        """执行一条已落账的删除意图: 本机保护先行, 再经中心 verify, 最后事务内删除并落结果。"""
        candidate = EvidenceDeletionCandidate(
            evidence_id=entry.evidence_id,
            generation=entry.generation,
            sha256=entry.observed_sha256,
            size=entry.observed_size,
            reference=entry.reference,
        )
        protection = self._local_protection(entry.station_id, entry.instance_id)
        if protection is not None:
            return self._finish(entry, EvidenceDeletionOutcome.RETAINED, protection)
        try:
            verified = self._transport.verify(
                EvidenceDeletionVerifyRequest(
                    host_id=self._host_id,
                    ticket=entry.ticket,
                    config_revision=revision,
                    effective_sha256=digest,
                    candidate=candidate,
                )
            )
        except EvidenceDeletionTransportError:
            self._state.record_deletion_failure(
                entry.evidence_id, error="center_unreachable", at=self._clock()
            )
            return EvidenceDeletionExecution(entry.evidence_id, "skipped", "center_unreachable")
        # 不得从 bool 推断资格: 响应必须绑定本账本的同一条认领与证据。
        if verified.claim_id != entry.claim_id or verified.evidence_id != entry.evidence_id:
            return self._finish(entry, EvidenceDeletionOutcome.CANCELLED, "verify_binding_mismatch")
        # 资格票据必须是可用的未来时刻(相对于认领创建), 而不是已过期的旧票;
        # 当前时刻的新鲜复核在材料哈希之后、取消链接之前再做一次。
        if verified.expires_at is None or verified.expires_at.timestamp() <= entry.created_at:
            return self._finish(entry, EvidenceDeletionOutcome.CANCELLED, "ticket_expired")
        if verified.decision is not EvidenceDeletionDecision.ELIGIBLE:
            return self._finish(entry, EvidenceDeletionOutcome.RETAINED, verified.reason)
        expires_at = verified.expires_at.timestamp()
        path = _contained_path(self._directory, candidate.reference)
        local_outcome: str | None = None
        with self._state.guard():
            confirmed = self._state.configuration().confirmed()
            if (
                confirmed is None
                or confirmed.host_id != self._host_id
                or confirmed.config_revision != revision
                or confirmed.effective_sha256 != digest
            ):
                action, reason = EvidenceDeletionOutcome.CANCELLED, "configuration_changed"
            else:
                protection = self._local_protection(entry.station_id, entry.instance_id)
                if protection is not None:
                    action, reason = EvidenceDeletionOutcome.RETAINED, protection
                elif path is None:
                    action, reason = EvidenceDeletionOutcome.CANCELLED, "reference_out_of_scope"
                elif not path.exists():
                    # 无法证明本认领成功取消链接, 但源文件确证不存在: 按不可用结案, 不写成已删除。
                    action, reason = EvidenceDeletionOutcome.MISSING, "source_missing"
                    local_outcome = "missing"
                elif not _material_matches(path, candidate.sha256, candidate.size):
                    action, reason = EvidenceDeletionOutcome.CANCELLED, "material_changed"
                elif self._clock() >= expires_at:
                    # 材料校验(哈希)之后、取消链接之前再验一次新鲜到期: 过期即拒绝。
                    action, reason = EvidenceDeletionOutcome.CANCELLED, "ticket_expired"
                else:
                    try:
                        path.unlink()
                    except OSError:
                        action, reason = EvidenceDeletionOutcome.CANCELLED, "unlink_failed"
                        local_outcome = "failed"
                    else:
                        action, reason = EvidenceDeletionOutcome.DELETED, "retention_expired"
                        local_outcome = None
            # 取消链接与终局结果写在同一事务: 崩溃时账本仍停在 claimed, 不发明成功。
            self._state.record_deletion_result(
                entry.evidence_id,
                outcome=action.value,
                reason_code=reason,
                detail=None,
                at=self._clock(),
            )
        return self._report_and_clear(entry, action, reason, local_outcome=local_outcome)

    def _finish(
        self, entry: EvidenceDeletionJournal, outcome: EvidenceDeletionOutcome, reason: str
    ) -> EvidenceDeletionExecution:
        """本机/中心任一事实已给出终局且无需取消链接: 落结果并反馈。"""
        with self._state.guard():
            self._state.record_deletion_result(
                entry.evidence_id,
                outcome=outcome.value,
                reason_code=reason,
                detail=None,
                at=self._clock(),
            )
        return self._report_and_clear(entry, outcome, reason)

    def _report_and_clear(
        self,
        entry: EvidenceDeletionJournal,
        outcome: EvidenceDeletionOutcome,
        reason: str,
        *,
        local_outcome: str | None = None,
    ) -> EvidenceDeletionExecution:
        """把终局结果反馈中心; 成功后清账本行, 失败保留供下一轮重放, 不制造删除。"""
        if not self._report(entry.claim_id, entry.evidence_id, outcome, reason):
            self._state.record_deletion_failure(
                entry.evidence_id, error="result_pending", at=self._clock()
            )
            return EvidenceDeletionExecution(entry.evidence_id, "skipped", "result_pending")
        self._state.clear_deletion_intent(entry.evidence_id)
        return EvidenceDeletionExecution(
            entry.evidence_id,
            local_outcome or _LOCAL_OUTCOME[outcome],
            reason,
        )

    def _report(
        self, claim_id: str, evidence_id: str, outcome: EvidenceDeletionOutcome, reason: str
    ) -> bool:
        """反馈终局返回是否已被中心接受; 中心暂时不可达时保留未结案账本等待重放。"""
        try:
            self._transport.report(
                EvidenceDeletionResultRequest(
                    host_id=self._host_id,
                    claim_id=claim_id,
                    evidence_id=evidence_id,
                    outcome=outcome,
                    reason_code=reason,
                    detail=None,
                )
            )
        except EvidenceDeletionTransportError:
            return False
        return True

    def _local_protection(self, station_id: str, instance_id: int) -> str | None:
        return self._state.station(station_id).evidence_deletion_protection(instance_id=instance_id)

    def _local_candidates(
        self,
        *,
        limit: int | None = None,
    ) -> Iterator[tuple[str, int, EvidenceDeletionCandidate]]:
        """只读本机产物索引; 周期入口有界分页并按需计算媒体身份, 不扫描整个目录。"""
        records = (
            self._state.registered_evidence_batch(limit=limit)
            if limit is not None
            else (
                record
                for station in self._state.registered_evidence_stations()
                for record in self._state.station(station).registered_evidence_media()
            )
        )
        for record in records:
            identity = record.identity or (
                None
                if record.queue_id is None
                else evidence_media_identity(self._host_id, record.queue_id)
            )
            if identity is None:
                continue
            artifact = evidence_artifact_directory(identity, record.covered_from, record.covered_to)
            for candidate in _candidates_for(self._directory, artifact, record.media_results):
                yield record.station_id, record.instance_id, candidate


def _outcome_of(value: str | None) -> EvidenceDeletionOutcome | None:
    if value is None:
        return None
    try:
        return EvidenceDeletionOutcome(value)
    except ValueError:
        return None


def _candidates_for(
    directory: Path, artifact: str, media_results: str
) -> Iterator[EvidenceDeletionCandidate]:
    """从已定稿产物的 typed 元数据构造候选; 只返回磁盘上真实存在且身份可读的文件。"""
    try:
        media = json.loads(media_results)
    except (TypeError, json.JSONDecodeError):
        return
    if not isinstance(media, list):
        return
    for item in media:
        if not isinstance(item, dict):
            continue
        generation = item.get("material_generation")
        if not isinstance(generation, str) or not generation:
            continue
        for field in ("clip_file", "keyframe_file"):
            name = item.get(field)
            if not isinstance(name, str) or not name:
                continue
            reference = f"{artifact}/{name}"
            path = _contained_path(directory, reference)
            if path is None or not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            digest = _digest(path)
            if not digest:
                continue
            yield EvidenceDeletionCandidate(
                evidence_id=reference,
                generation=generation,
                sha256=digest,
                size=size,
                reference=reference,
            )


def _contained_path(base: Path, reference: str) -> Path | None:
    """把中心引用解析为本机证据目录内的两级路径; 越界或非法引用返回 ``None``。"""
    parts = reference.split("/")
    if len(parts) != 2 or not all(part and part not in (".", "..") for part in parts):
        return None
    candidate = base / parts[0] / parts[1]
    try:
        if not candidate.resolve().is_relative_to(base.resolve()):
            return None
    except OSError:
        return None
    return candidate


def _material_matches(path: Path, sha256: str, size: int) -> bool:
    """本机文件必须仍等于候选声明的媒体身份; 不一致即不删。"""
    try:
        if path.stat().st_size != size:
            return False
    except OSError:
        return False
    return _digest(path) == sha256.lower()


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


__all__ = [
    "EvidenceDeletionExecution",
    "EvidenceDeletionExecutor",
    "EvidenceDeletionTransport",
    "EvidenceDeletionTransportError",
]
