#!/usr/bin/env python3
"""自动 PR 落地队列：以 GitHub 为持久真源的单飞 FIFO 编排器。

仓库是唯一真源：状态 JSON 在 `landing-queue-state` 分支，用 Contents API blob SHA 做 CAS；
入队/出队唤醒请求在一个追加式 issue 评论 inbox。没有守护进程、锁服务、数据库或轮询。
可信的 landing-queue.yml 运行默认分支代码，是状态分支的唯一写入者；CLI 从不写状态。

信任边界：请求评论必须由具备 write/maintain/admin 权限的人类撰写且不可编辑；授权不是布尔值，
而是对一条已存在的、人类维护者撰写的 PR 佐证评论的引用（comment id + digest + author）。
队列只做受控集成（一次 refresh、一次 CI、一次 merge），绝不本地 rebase/reset/解冲突/修代码/重试。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import land_pr
from bind_task_session import (
    BindingError,
    binding_path,
    canonical_worktree,
    current_branch,
    current_worktree,
    load_binding,
)

VERSION = 3
STATE_BRANCH = "landing-queue-state"
STATE_PATH = "queue.json"
INBOX_ISSUE_ENV = "LANDING_QUEUE_ISSUE"
APP_ID_ENV = "LANDING_APP_ID"
APP_BOT_ENV = "LANDING_APP_BOT"
CONTROLLER_PATH = ".github/workflows/landing-queue.yml"
REUSABLE_CI_PATH = ".github/workflows/blocking-ci.yml"
REQUEST_MARKER = "<!-- landing-queue-request -->"
ATTESTATION_MARKER = "<!-- landing-attestation -->"
TRUSTED_EVENTS = ("pull_request_target", "issue_comment", "workflow_dispatch", "push")
QUEUED, ACTIVE, BLOCKED, MERGED, CANCELLED = "QUEUED", "ACTIVE", "BLOCKED", "MERGED", "CANCELLED"
STATES = frozenset({QUEUED, ACTIVE, BLOCKED, MERGED, CANCELLED})
TERMINAL = frozenset({BLOCKED, MERGED, CANCELLED})
REFRESHING, TESTING, MERGING = "REFRESHING", "TESTING", "MERGING"
PHASES = frozenset({REFRESHING, TESTING, MERGING})
BLOCKED_CI = "BLOCKED_CI"
BLOCKED_CONFLICT = "BLOCKED_CONFLICT"
BLOCKED_REVIEW = "BLOCKED_REVIEW"
BLOCKED_MUTATION = "BLOCKED_MUTATION"
BLOCKED_SCOPE = "BLOCKED_SCOPE"
BLOCKED_AUTHORITY = "BLOCKED_AUTHORITY"
BLOCKED_INFRA = "BLOCKED_INFRA"
BLOCKED_REASONS = frozenset(
    {
        BLOCKED_CI,
        BLOCKED_CONFLICT,
        BLOCKED_REVIEW,
        BLOCKED_MUTATION,
        BLOCKED_SCOPE,
        BLOCKED_AUTHORITY,
        BLOCKED_INFRA,
    }
)
INFRA_CONCLUSIONS = frozenset(
    {"cancelled", "timed_out", "startup_failure", "stale", "action_required"}
)
# 可复用 blocking-ci.yml 的真实作业名；控制器自身的 prepare/finalize 不在内，
# 因此运行中的 finalizer 不会被误当作 CI 结论。
REUSABLE_CI_JOB_NAMES = frozenset(
    {
        "Change scope",
        "Lockfile cleanliness",
        "Repository checks",
        "center-integration evidence",
        "center-system evidence",
        "Chrome and Edge system evidence",
        "CI required",
    }
)
IN_FLIGHT = frozenset({"queued", "in_progress", "requested", "waiting", "pending"})
# 公开状态字段是固定白名单：不含运行时 session 身份，也不含 worktree 路径。
ENTRY_KEYS = (
    "pr",
    "state",
    "phase",
    "request_id",
    "handoff",
    "candidate",
    "authorization_root",
    "attestation_comment",
    "attestation_digest",
    "attestation_author",
    "refresh_root",
    "refresh_base",
    "ci_run_id",
    "ci_attempt",
    "ci_controller",
    "ci_head",
    "ci_base",
    "merge_commit",
    "merge_tree",
    "blocked_reason",
    "evidence",
)
STATE_KEYS = ("version", "consumed_request_id", "entries")
EVIDENCE_KEYS = (
    "handoff",
    "reason",
    "category",
    "expected_head",
    "observed_head",
    "main",
    "run",
    "attempt",
)
# 修复协调：只有候选缺陷类阻塞会回到实现会话；AUTHORITY/INFRA 不是代码修复。
REPAIR_CLAIM, REPAIR_RESUMING, REPAIR_RESUMED, REPAIR_UNAVAILABLE = (
    "repair-claim",
    "repair-resuming",
    "repair-resumed",
    "repair-unavailable",
)
REPAIR_KINDS = frozenset({REPAIR_CLAIM, REPAIR_RESUMING, REPAIR_RESUMED, REPAIR_UNAVAILABLE})
REPAIR_REASONS = BLOCKED_REASONS - {BLOCKED_AUTHORITY, BLOCKED_INFRA}
# claimed/resuming 未决；resumed/unavailable 终态，只有终态才允许带 token 重入队。
REPAIR_STATES = frozenset({"claimed", "resuming", "resumed", "unavailable"})
REPAIR_PENDING_STATES = frozenset({"claimed", "resuming"})
REPAIR_TERMINAL_STATES = REPAIR_STATES - REPAIR_PENDING_STATES
# 修复键与失败证据共用 evidence 白名单：公开但不含 session 身份或 worktree 路径。
REPAIR_KEYS = ("repair_state", "repair_claim", "repair_generation")
FULL_OBJECT_ID = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
HANDOFF_ID = re.compile(r"^[0-9a-f]{32}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
ATTESTATION_ACTIONS = ("refresh", "squash-merge")
WRITE_PERMISSIONS = frozenset({"write", "maintain", "admin"})


class QueueError(RuntimeError):
    """前置条件或状态转移 fail closed。"""


class InfrastructureError(QueueError):
    """运行时基础设施失败：可恢复，绝不能当作候选代码缺陷。"""


class ProtectionError(QueueError):
    """服务器端保护缺失：在写状态或发布 status 之前 fail closed。"""


class StateMissingError(QueueError):
    """状态分支/快照缺失：不是空队列成功。"""


class MissingSourceError(QueueError):
    """被引用的 PR/评论已不存在（404）：确定事实，跳过请求并推进水位。"""


# --------------------------------------------------------------------------- 纯状态机


@dataclass(frozen=True)
class Entry:
    pr: int
    state: str
    phase: str | None
    request_id: int
    handoff: str
    candidate: str
    authorization_root: str
    attestation_comment: int
    attestation_digest: str
    attestation_author: str
    refresh_root: str | None = None
    refresh_base: str | None = None
    ci_run_id: str | None = None
    ci_attempt: str | None = None
    ci_controller: str | None = None
    ci_head: str | None = None
    ci_base: str | None = None
    merge_commit: str | None = None
    merge_tree: str | None = None
    blocked_reason: str | None = None
    evidence: dict[str, str] | None = None


@dataclass(frozen=True)
class State:
    consumed_request_id: int = 0
    entries: tuple[Entry, ...] = ()


@dataclass(frozen=True)
class Request:
    kind: str
    pr: int
    request_id: int
    candidate: str = ""
    handoff: str = ""
    attestation_comment: int = 0
    attestation_digest: str = ""
    # claim 是修复声明/完成 token 的 sha256 digest；明文只留在本地 stdout。
    claim: str = ""
    generation: str = ""


@dataclass(frozen=True)
class Comment:
    id: int
    body: str
    author: str
    author_type: str
    created_at: str
    updated_at: str
    issue_url: str


@dataclass(frozen=True)
class Attestation:
    comment: int
    digest: str
    author: str
    root: str
    pr: int
    head_branch: str


@dataclass(frozen=True)
class RefreshEvent:
    before: str
    after: str
    actor: str
    pr: int


def find(state: State, pr: int) -> Entry | None:
    return next((entry for entry in state.entries if entry.pr == pr), None)


def active(state: State) -> Entry | None:
    return next((entry for entry in state.entries if entry.state == ACTIVE), None)


def ordered(state: State) -> tuple[Entry, ...]:
    """按服务器 request id FIFO；PR 号只做确定性 tie-break。"""
    return tuple(sorted(state.entries, key=lambda entry: (entry.request_id, entry.pr)))


def promote(state: State) -> State:
    if active(state) is not None:
        return state
    queued = [entry for entry in ordered(state) if entry.state == QUEUED]
    if not queued:
        return state
    chosen = queued[0]
    return replace(
        state, entries=tuple(replace(e, state=ACTIVE) if e is chosen else e for e in state.entries)
    )


def _new_entry(request: Request, attestation: Attestation) -> Entry:
    return Entry(
        pr=request.pr,
        state=QUEUED,
        phase=None,
        request_id=request.request_id,
        handoff=request.handoff,
        candidate=attestation.root,
        authorization_root=attestation.root,
        attestation_comment=attestation.comment,
        attestation_digest=attestation.digest,
        attestation_author=attestation.author,
    )


def _replace_entry(current: State, pr: int, **changes: object) -> State:
    entries = tuple(
        replace(entry, **changes) if entry.pr == pr else entry  # type: ignore[arg-type]
        for entry in current.entries
    )
    return replace(current, entries=entries)


def apply_enqueue(state: State, request: Request, attestation: Attestation) -> State:
    _validate_request_inputs(request)
    existing = find(state, request.pr)
    if existing is not None and existing.state in {ACTIVE, QUEUED}:
        if existing.authorization_root == attestation.root:
            return state
        if existing.state == QUEUED:
            # 不同候选不得静默替换授权根：先 BLOCKED_MUTATION，修复后显式重新入队到队尾。
            return block(state, request.pr, BLOCKED_MUTATION)
        raise QueueError(f"pr {request.pr} is ACTIVE with a different authorization root")
    repair = _repair_state(existing) if existing is not None and existing.state == BLOCKED else None
    if repair in REPAIR_STATES:
        # 只有终态声明可被带 token 的重新入队释放；claimed/resuming 未决时一律拒绝，避免双写入者。
        claim = (existing.evidence or {}).get("repair_claim")
        if repair not in REPAIR_TERMINAL_STATES or request.claim != claim:
            raise QueueError("a repair claim is active; re-enqueue needs its completion token")
    entries = tuple(entry for entry in state.entries if entry.pr != request.pr)
    return promote(replace(state, entries=(*entries, _new_entry(request, attestation))))


def apply_dequeue(state: State, request: Request) -> State:
    existing = find(state, request.pr)
    if existing is None or existing.state in TERMINAL:
        return state
    if existing.phase == MERGING:
        # 合并意图已在途：不得取消并擦除证明；留给 _prove_or_recover 证明结果。
        return state
    # QUEUED 与 ACTIVE 都保留可观察的 CANCELLED；ACTIVE 出队释放落地写入者。
    return promote(
        _replace_entry(
            state, request.pr, state=CANCELLED, phase=None, blocked_reason=None, evidence=None
        )
    )


def block(state: State, pr: int, reason: str, evidence: dict[str, str] | None = None) -> State:
    if reason not in BLOCKED_REASONS:
        raise QueueError(f"unknown blocked reason: {reason}")
    existing = find(state, pr)
    if existing is None or existing.state in TERMINAL:
        raise QueueError(f"pr {pr} is not blocked-able")
    # 先保存证据再清空 intent：崩溃恢复不会丢失失败事实。
    merged = {**(existing.evidence or {}), **(evidence or {}), "reason": reason}
    merged["category"] = "infrastructure" if reason == BLOCKED_INFRA else "candidate"
    return promote(
        _replace_entry(
            state,
            pr,
            state=BLOCKED,
            phase=None,
            blocked_reason=reason,
            evidence=merged,
            ci_run_id=None,
            ci_attempt=None,
            ci_controller=None,
            ci_head=None,
            ci_base=None,
            refresh_root=None,
            refresh_base=None,
        )
    )


def begin_refresh(state: State, pr: int, old_head: str, base: str) -> State:
    if _require_active(state, pr).candidate != old_head:
        raise QueueError("refresh intent must freeze the current ACTIVE candidate")
    return _replace_entry(state, pr, phase=REFRESHING, refresh_root=old_head, refresh_base=base)


def accept_refresh(state: State, pr: int, new_head: str, base: str) -> State:
    existing = _require_active(state, pr)
    if existing.phase != REFRESHING:
        raise QueueError("refresh acceptance requires a recorded REFRESHING intent")
    if base != existing.refresh_base:
        raise QueueError("refresh base changed; refusing an ambiguous head")
    if FULL_OBJECT_ID.fullmatch(new_head) is None:
        raise QueueError("refreshed head must be a full Git object id")
    return _replace_entry(
        state, pr, candidate=new_head, phase=None, refresh_root=None, refresh_base=None
    )


def begin_testing(
    state: State, pr: int, run_id: str, attempt: str, controller: str, head: str, base: str
) -> State:
    existing = _require_active(state, pr)
    if existing.candidate != head:
        raise QueueError("CI intent must freeze the current ACTIVE candidate")
    if existing.phase == TESTING:
        if (existing.ci_run_id, existing.ci_attempt, existing.ci_controller, existing.ci_head) == (
            run_id,
            attempt,
            controller,
            head,
        ):
            return state
        raise QueueError("CI already running for a different run id")
    if existing.phase is not None:
        raise QueueError(f"cannot start CI during phase {existing.phase}")
    return _replace_entry(
        state,
        pr,
        phase=TESTING,
        ci_run_id=run_id,
        ci_attempt=attempt,
        ci_controller=controller,
        ci_head=head,
        ci_base=base,
    )


def begin_merge(state: State, pr: int, head: str) -> State:
    existing = _require_active(state, pr)
    if existing.phase != TESTING or existing.ci_head != head:
        raise QueueError("merge intent requires the tested head")
    return _replace_entry(state, pr, phase=MERGING, merge_commit=None, merge_tree=None)


def complete(state: State, pr: int, merge_commit: str, merge_tree: str) -> State:
    if FULL_OBJECT_ID.fullmatch(merge_commit) is None:
        raise QueueError("merge commit must be a full Git object id")
    _require_active(state, pr)
    return promote(
        _replace_entry(
            state, pr, state=MERGED, phase=None, merge_commit=merge_commit, merge_tree=merge_tree
        )
    )


def _require_active(state: State, pr: int) -> Entry:
    existing = find(state, pr)
    if existing is None or existing.state != ACTIVE:
        raise QueueError(f"pr {pr} is not ACTIVE")
    return existing


def _repair_state(entry: Entry) -> str | None:
    return (entry.evidence or {}).get("repair_state")


def claim_digest(token: str) -> str:
    """公开状态只存 token 的 sha256；明文 token 只留在本地调用方手里。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def repair_generation(entry: Entry) -> str:
    evidence = entry.evidence or {}
    keys = ("expected_head", "observed_head", "main", "run", "attempt")
    parts = [entry.handoff, entry.blocked_reason, entry.candidate, entry.authorization_root]
    parts += [evidence.get(key, "") for key in keys]
    return hashlib.sha256("\x1f".join(part or "" for part in parts).encode("utf-8")).hexdigest()


def repair_event(backend: Backend, pr: int) -> dict[str, object]:
    """从当前 BLOCKED 条目和实际 PR/佐证派生公开修复事件；过期或不匹配即拒绝。"""
    state, _ = backend.state()
    entry = find(state, pr)
    if entry is None or entry.state != BLOCKED or entry.phase is not None:
        raise QueueError("no blocked landing entry to repair")
    if entry.blocked_reason not in REPAIR_REASONS:
        raise QueueError(f"{entry.blocked_reason} is not a code-repair handoff")
    evidence = entry.evidence or {}
    if not set(EVIDENCE_KEYS).issubset(evidence):
        raise QueueError("blocked entry is missing its failure evidence")
    repository = backend.repository()
    comment_obj = backend.comment(entry.attestation_comment)
    attestation = attestation_from_comment(comment_obj, repository, pr)
    if not _trusted(backend, comment_obj):
        raise QueueError("attestation is not a trusted human maintainer comment")
    pull = backend.pull_request(pr)
    if pull.state != "OPEN" or pull.base_name != "main":
        raise QueueError("pull request is not open against main")
    if attestation.head_branch != pull.head_name:
        raise QueueError("attestation branch does not match the pull request")
    if evidence["observed_head"] != pull.head_oid:
        raise QueueError("pull-request head moved since the block; the event is stale")
    if evidence["main"] != backend.main_sha():
        raise QueueError("main moved since the block; the event is stale")
    location = {"state_branch": STATE_BRANCH, "path": STATE_PATH}
    location |= {"run": evidence["run"], "attempt": evidence["attempt"]}
    event = {"version": 1, "event": repair_generation(entry), "handoff": entry.handoff}
    event |= {"repository": repository, "pr": pr, "branch": attestation.head_branch}
    event |= {"blocked_state": entry.blocked_reason, "authorization_root": entry.authorization_root}
    event |= {"landing_head": evidence["expected_head"], "main": evidence["main"]}
    event |= {"failure": {key: evidence[key] for key in ("reason", "category", "run", "attempt")}}
    event |= {"evidence": {key: evidence[key] for key in EVIDENCE_KEYS}}
    event |= {"evidence_location": location}
    return event


def apply_repair(state: State, request: Request) -> State:
    """独占修复声明与其结果；同一代次只接受一次，旧/重复请求 fail closed。"""
    _validate_repair_request(request)
    entry = find(state, request.pr)
    if entry is None or entry.state != BLOCKED or entry.phase is not None:
        raise QueueError("no blocked entry to repair")
    if entry.blocked_reason not in REPAIR_REASONS:
        raise QueueError("this blocked reason is not a code-repair handoff")
    if entry.handoff != request.handoff:
        raise QueueError("repair request does not name the blocked generation")
    if repair_generation(entry) != request.generation:
        raise QueueError("repair event is stale")
    evidence = dict(entry.evidence or {})
    current = evidence.get("repair_state")
    # 请求里的 claim 已是 sha256 digest；控制器无需明文 token。
    digest = request.claim
    if request.kind == REPAIR_CLAIM:
        if current in REPAIR_STATES:
            raise QueueError("a repair claim already exists for this generation")
        evidence["repair_state"] = "claimed"
        evidence["repair_claim"] = digest
        evidence["repair_generation"] = request.generation
    elif request.kind == REPAIR_RESUMING:
        # 只有已接受的 claim 能推进 claimed→resuming；resuming 期间重入队/二次声明全拒。
        if current != "claimed" or evidence.get("repair_claim") != digest:
            raise QueueError("repair resume does not match the accepted claim")
        evidence["repair_state"] = "resuming"
    else:
        # 结果只接受未决声明上的终态转换；token digest 必须匹配。
        if current not in REPAIR_PENDING_STATES or evidence.get("repair_claim") != digest:
            raise QueueError("repair result does not match the accepted claim")
        evidence["repair_state"] = "resumed" if request.kind == REPAIR_RESUMED else "unavailable"
    return _replace_entry(state, request.pr, evidence=evidence)


def _validate_repair_request(request: Request) -> None:
    if not isinstance(request.pr, int) or isinstance(request.pr, bool) or request.pr <= 0:
        raise QueueError("pr must be a positive integer")
    if not isinstance(request.request_id, int) or request.request_id <= 0:
        raise QueueError("request id must be a positive integer")
    if HANDOFF_ID.fullmatch(request.handoff) is None:
        raise QueueError("repair request must name an opaque 32-hex handoff")
    if DIGEST.fullmatch(request.claim) is None:
        raise QueueError("repair request must name its 64-hex claim digest")
    if DIGEST.fullmatch(request.generation) is None:
        raise QueueError("repair request must name its 64-hex event generation")


def _validate_request_inputs(request: Request) -> None:
    if not isinstance(request.pr, int) or isinstance(request.pr, bool) or request.pr <= 0:
        raise QueueError("pr must be a positive integer")
    if FULL_OBJECT_ID.fullmatch(request.candidate) is None:
        raise QueueError("candidate must be a full Git object id")
    if not isinstance(request.request_id, int) or request.request_id <= 0:
        raise QueueError("request id must be a positive integer")
    if HANDOFF_ID.fullmatch(request.handoff) is None:
        raise QueueError("handoff id must be an opaque 32-hex token")
    if not isinstance(request.attestation_comment, int) or request.attestation_comment <= 0:
        raise QueueError("attestation comment id must be a positive integer")
    if DIGEST.fullmatch(request.attestation_digest) is None:
        raise QueueError("attestation digest must be a sha256 hex digest")
    if request.claim and DIGEST.fullmatch(request.claim) is None:
        raise QueueError("repair completion token must be a sha256 digest")


# --------------------------------------------------------------------------- 序列化


def _int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise QueueError(f"{name} must be an integer")
    return value


def _str(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise QueueError(f"{name} must be a non-empty string")
    return value


def _opt(value: object, name: str) -> str | None:
    return None if value is None else _str(value, name)


def _evidence(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not set(value).issubset(
        set(EVIDENCE_KEYS) | set(REPAIR_KEYS)
    ):
        raise QueueError("evidence must be an opaque documented object")
    result = {str(key): _str(item, f"evidence.{key}") for key, item in value.items()}
    if result.get("repair_state", "claimed") not in REPAIR_STATES:
        raise QueueError("unknown repair state")
    for key in ("repair_claim", "repair_generation"):
        if key in result and DIGEST.fullmatch(result[key]) is None:
            raise QueueError(f"evidence.{key} must be a sha256 hex digest")
    return result


def entry_from_json(payload: object) -> Entry:
    if not isinstance(payload, dict) or set(payload) != set(ENTRY_KEYS):
        raise QueueError("queue entry must have exactly the documented keys")
    state = _str(payload["state"], "state")
    if state not in STATES:
        raise QueueError(f"unknown queue state: {state}")
    phase = _opt(payload["phase"], "phase")
    if phase is not None and phase not in PHASES:
        raise QueueError(f"unknown queue phase: {phase}")
    reason = _opt(payload["blocked_reason"], "blocked_reason")
    if reason is not None and reason not in BLOCKED_REASONS:
        raise QueueError(f"unknown blocked reason: {reason}")
    return Entry(
        pr=_int(payload["pr"], "pr"),
        state=state,
        phase=phase,
        request_id=_int(payload["request_id"], "request_id"),
        handoff=_str(payload["handoff"], "handoff"),
        candidate=_str(payload["candidate"], "candidate"),
        authorization_root=_str(payload["authorization_root"], "authorization_root"),
        attestation_comment=_int(payload["attestation_comment"], "attestation_comment"),
        attestation_digest=_str(payload["attestation_digest"], "attestation_digest"),
        attestation_author=_str(payload["attestation_author"], "attestation_author"),
        refresh_root=_opt(payload["refresh_root"], "refresh_root"),
        refresh_base=_opt(payload["refresh_base"], "refresh_base"),
        ci_run_id=_opt(payload["ci_run_id"], "ci_run_id"),
        ci_attempt=_opt(payload["ci_attempt"], "ci_attempt"),
        ci_controller=_opt(payload["ci_controller"], "ci_controller"),
        ci_head=_opt(payload["ci_head"], "ci_head"),
        ci_base=_opt(payload["ci_base"], "ci_base"),
        merge_commit=_opt(payload["merge_commit"], "merge_commit"),
        merge_tree=_opt(payload["merge_tree"], "merge_tree"),
        blocked_reason=reason,
        evidence=_evidence(payload["evidence"]),
    )


def entry_to_json(entry: Entry) -> dict[str, object]:
    return {key: getattr(entry, key) for key in ENTRY_KEYS}


def state_from_json(payload: object) -> State:
    if not isinstance(payload, dict) or set(payload) != set(STATE_KEYS):
        raise QueueError("queue state must have exactly version, consumed_request_id and entries")
    if _int(payload["version"], "version") != VERSION:
        raise QueueError("unsupported queue state version")
    entries = payload["entries"]
    if not isinstance(entries, list):
        raise QueueError("queue entries must be a list")
    parsed = tuple(entry_from_json(item) for item in entries)
    if len({entry.pr for entry in parsed}) != len(parsed):
        raise QueueError("queue state has duplicate pull requests")
    if sum(1 for entry in parsed if entry.state == ACTIVE) > 1:
        raise QueueError("queue state has more than one ACTIVE entry")
    return State(_int(payload["consumed_request_id"], "consumed_request_id"), parsed)


def state_to_json(state: State) -> dict[str, object]:
    return {
        "version": VERSION,
        "consumed_request_id": state.consumed_request_id,
        "entries": [entry_to_json(entry) for entry in state.entries],
    }


# --------------------------------------------------------------------------- inbox / 佐证解析


def _fenced_json(body: str, marker: str) -> dict[str, Any]:
    index = body.find(marker)
    if index < 0:
        raise QueueError(f"comment is missing the {marker} marker")
    match = re.search(r"```json\n(.*?)\n```", body[index:], re.DOTALL)
    if match is None:
        raise QueueError("comment is missing its JSON payload")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise QueueError("comment JSON payload is invalid") from exc
    if not isinstance(payload, dict):
        raise QueueError("comment payload must be an object")
    return payload


def request_body(request: Request) -> str:
    payload = {
        "version": VERSION,
        "kind": request.kind,
        "pr": request.pr,
        "candidate": request.candidate,
        "handoff": request.handoff,
        "attestation_comment": request.attestation_comment,
        "attestation_digest": request.attestation_digest,
        "claim": request.claim,
        "generation": request.generation,
    }
    return f"{REQUEST_MARKER}\n```json\n{json.dumps(payload, sort_keys=True)}\n```\n"


def request_from_comment(comment: Comment) -> Request | None:
    if REQUEST_MARKER not in comment.body:
        return None
    payload = _fenced_json(comment.body, REQUEST_MARKER)
    if payload.get("version") != VERSION:
        raise QueueError("inbox request has an unsupported version")
    kind = payload.get("kind")
    if kind not in {"enqueue", "dequeue", *REPAIR_KINDS}:
        raise QueueError("inbox request has an unknown kind")
    return Request(
        kind=kind,
        pr=_int(payload.get("pr"), "pr"),
        request_id=comment.id,
        candidate=str(payload.get("candidate") or ""),
        handoff=str(payload.get("handoff") or ""),
        attestation_comment=_int(payload.get("attestation_comment") or 0, "attestation_comment"),
        attestation_digest=str(payload.get("attestation_digest") or ""),
        claim=str(payload.get("claim") or ""),
        generation=str(payload.get("generation") or ""),
    )


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and value.strip() != ""


def attestation_from_comment(comment: Comment, repository: str, number: int) -> Attestation:
    """解析并校验人类维护者佐证；只保留消费端真正读取的字段。

    佐证引用一条真实存在的人类确认与已执行的证据，不制造布尔式授权；这里只校验引用的
    结构与精确根，人工语义审查仍是信任边界。
    """
    if ATTESTATION_MARKER not in comment.body:
        raise QueueError("attestation comment is missing its marker")
    payload = _fenced_json(comment.body, ATTESTATION_MARKER)
    if payload.get("version") != 1:
        raise QueueError("attestation has an unsupported version")
    if payload.get("repository") != repository:
        raise QueueError("attestation repository does not match")
    if _int(payload.get("pr"), "attestation pr") != number:
        raise QueueError("attestation does not name this pull request")
    if payload.get("base") != "main":
        raise QueueError("attestation base must be main")
    root = payload.get("root")
    if not isinstance(root, str) or FULL_OBJECT_ID.fullmatch(root) is None:
        raise QueueError("attestation root must be a full Git object id")
    actions = payload.get("actions")
    if not isinstance(actions, list) or not set(ATTESTATION_ACTIONS).issubset(actions):
        raise QueueError("attestation must authorize refresh and squash-merge")
    confirmation = payload.get("confirmation")
    if (
        not isinstance(confirmation, dict)
        or not _nonempty(confirmation.get("source"))
        or not _nonempty(confirmation.get("quote"))
    ):
        raise QueueError("attestation must quote the prior human confirmation source and text")
    check = payload.get("check")
    if (
        not isinstance(check, dict)
        or check.get("root") != root
        or check.get("result") != "passed"
        or check.get("command") != "make check"
        or not _nonempty(check.get("evidence"))
    ):
        raise QueueError("attestation must carry exact-root passed make check evidence reference")
    review = payload.get("review")
    if not isinstance(review, dict):
        raise QueueError("attestation must carry review evidence")
    na = review.get("not-applicable")
    justified = isinstance(na, str) and na.strip() != ""
    if not justified and not (
        review.get("spec") == "passed"
        and review.get("standards") == "passed"
        and review.get("root") == root
        and _nonempty(review.get("evidence"))
    ):
        raise QueueError("attestation must carry independent review evidence or a justification")
    return Attestation(
        comment=comment.id,
        digest=hashlib.sha256(comment.body.encode("utf-8")).hexdigest(),
        author=comment.author,
        root=root,
        pr=number,
        head_branch=_str(payload.get("head_branch"), "attestation head_branch"),
    )


def comment_is_trusted(comment: Comment, permission: str) -> bool:
    """人类维护者（write/maintain/admin）撰写且未被编辑过的评论才可作请求或佐证。"""
    return (
        comment.author_type.lower() != "bot"
        and comment.created_at != ""
        and comment.created_at == comment.updated_at
        and permission in WRITE_PERMISSIONS
    )


# --------------------------------------------------------------------------- 后端边界


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
Backend = "GitHub"


def _run_gh(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["gh", *arguments], capture_output=True, text=True, check=False)
    except OSError as exc:
        raise InfrastructureError(f"cannot run gh: {exc}") from exc


def _json(runner: Runner, arguments: Sequence[str]) -> object:
    result = runner(arguments)
    if result.returncode != 0:
        raise InfrastructureError(result.stderr.strip() or "gh command failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise InfrastructureError("gh returned invalid JSON") from exc


def _comment_from(item: dict[str, Any]) -> Comment:
    user = item.get("user")
    if not isinstance(user, dict) or not isinstance(item.get("body"), str):
        raise InfrastructureError("comment response is malformed")
    return Comment(
        id=_int(item.get("id"), "comment id"),
        body=item["body"],
        author=_str(user.get("login"), "comment author"),
        author_type=str(user.get("type") or "User"),
        created_at=str(item.get("created_at") or ""),
        updated_at=str(item.get("updated_at") or ""),
        issue_url=str(item.get("issue_url") or ""),
    )


class GitHub:
    """真实 GitHub 适配器；测试只在 Backend 边界注入假实现。"""

    def __init__(self, runner: Runner = _run_gh) -> None:
        self._run = runner

    def _api(self, path: str, *extra: str) -> object:
        return _json(self._run, ["api", path, *extra])

    def _put(self, path: str, *fields: str) -> object:
        return _json(self._run, ["api", "--method", "PUT", path, *fields])

    def repository(self) -> str:
        payload = _json(self._run, ["repo", "view", "--json", "nameWithOwner"])
        name = payload.get("nameWithOwner") if isinstance(payload, dict) else None
        if not isinstance(name, str) or "/" not in name:
            raise InfrastructureError("repository nameWithOwner is missing")
        return name

    def state(self) -> tuple[State, str]:
        payload = self._api(f"repos/{self.repository()}/contents/{STATE_PATH}?ref={STATE_BRANCH}")
        if not isinstance(payload, dict) or not isinstance(payload.get("sha"), str):
            raise StateMissingError("queue state response is missing its blob sha")
        content = payload.get("content")
        if not isinstance(content, str):
            raise StateMissingError("queue state response is missing its content")
        try:
            return state_from_json(
                json.loads(base64.b64decode(content.replace("\n", "")))
            ), payload["sha"]
        except (json.JSONDecodeError, QueueError) as exc:
            raise StateMissingError(f"queue state is invalid: {exc}") from exc

    def write_state(self, state: State, revision: str) -> bool:
        content = base64.b64encode(
            json.dumps(state_to_json(state), sort_keys=True).encode()
        ).decode()
        try:
            self._put(
                f"repos/{self.repository()}/contents/{STATE_PATH}",
                "-f",
                f"branch={STATE_BRANCH}",
                "-f",
                "message=landing-queue: transition",
                "-f",
                f"content={content}",
                "-f",
                f"sha={revision}",
            )
        except InfrastructureError as exc:
            message = str(exc)
            if "409" in message or "does not match" in message:
                return False
            raise
        return True

    def _paginated(self, path: str) -> list[dict[str, Any]]:
        # --slurp 把多页包成数组的数组；flatten 后仍能处理 >100 条评论。
        payload = _json(self._run, ["api", "--paginate", "--slurp", path])
        if not isinstance(payload, list):
            raise InfrastructureError("paginated payload must be a list")
        items = [item for page in payload for item in (page if isinstance(page, list) else [page])]
        if not all(isinstance(item, dict) for item in items):
            raise InfrastructureError("paginated payload items must be objects")
        return items

    def inbox(self) -> list[Comment]:
        return [
            _comment_from(item)
            for item in self._paginated(
                f"repos/{self.repository()}/issues/{_inbox_issue()}/comments"
            )
        ]

    def comment(self, comment_id: int) -> Comment:
        try:
            return _comment_from(
                self._api(f"repos/{self.repository()}/issues/comments/{comment_id}")
            )
        except InfrastructureError as exc:
            # 404 是“评论已删除”的确定事实；其它错误是 API 传输故障。
            if _missing_source(str(exc)):
                raise MissingSourceError(f"comment {comment_id} does not exist") from exc
            raise

    def permission(self, login: str) -> str:
        payload = self._api(f"repos/{self.repository()}/collaborators/{login}/permission")
        value = payload.get("permission") if isinstance(payload, dict) else None
        return value if isinstance(value, str) else "none"

    def pull_request(self, number: int) -> land_pr.PullRequestState:
        try:
            return land_pr.pull_request(number)
        except land_pr.LandingError as exc:
            # 缺失 PR 是确定事实；真正的 API 故障显式转为基础设施错误。
            if _missing_source(str(exc)):
                raise MissingSourceError(f"pull request {number} does not exist") from exc
            raise InfrastructureError(f"pull request query failed: {exc}") from exc

    def main_sha(self) -> str:
        payload = self._api(f"repos/{self.repository()}/git/ref/heads/main")
        obj = payload.get("object") if isinstance(payload, dict) else None
        return land_pr.parse_object_id(
            obj.get("sha") if isinstance(obj, dict) else None, "main ref"
        )

    def commit_parents(self, sha: str) -> tuple[str, ...]:
        payload = self._api(f"repos/{self.repository()}/commits/{sha}")
        parents = payload.get("parents") if isinstance(payload, dict) else None
        if not isinstance(parents, list):
            raise InfrastructureError("commit parents are missing")
        return tuple(land_pr.parse_object_id(item.get("sha"), "parent") for item in parents)

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        payload = self._api(f"repos/{self.repository()}/compare/{ancestor}...{descendant}")
        status = payload.get("status") if isinstance(payload, dict) else None
        return status in {"ahead", "identical"}

    def tree_sha(self, sha: str) -> str:
        payload = self._api(f"repos/{self.repository()}/git/commits/{sha}")
        tree = payload.get("tree") if isinstance(payload, dict) else None
        return land_pr.parse_object_id(tree.get("sha") if isinstance(tree, dict) else None, "tree")

    def rulesets(self) -> list[dict[str, Any]]:
        repo = self.repository()
        summaries = self._api(f"repos/{repo}/rulesets")
        if not isinstance(summaries, list):
            raise InfrastructureError("ruleset list must be an array")
        active = [
            item
            for item in summaries
            if isinstance(item, dict) and item.get("enforcement") == "active"
        ]
        return [self._api(f"repos/{repo}/rulesets/{item['id']}") for item in active]

    def refresh(self, number: int, head: str) -> None:
        land_pr.refresh(number, head)

    def merge(self, number: int, head: str) -> str:
        return land_pr.merge(number, head)

    def run(self, run_id: str) -> dict[str, Any]:
        return self._api(f"repos/{self.repository()}/actions/runs/{run_id}")

    def jobs(self, run_id: str) -> list[dict[str, Any]]:
        payload = self._api(f"repos/{self.repository()}/actions/runs/{run_id}/jobs")
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            raise InfrastructureError("run jobs must be an array")
        return [job for job in jobs if isinstance(job, dict)]

    def publish_status(self, sha: str, context: str, state: str, description: str) -> None:
        result = self._run(
            [
                "api",
                "--method",
                "POST",
                f"repos/{self.repository()}/statuses/{sha}",
                "-f",
                f"state={state}",
                "-f",
                f"context={context}",
                "-f",
                f"description={description}",
            ]
        )
        if result.returncode != 0:
            raise InfrastructureError(result.stderr.strip() or f"cannot publish {context}")

    def post_request(self, body: str) -> int:
        payload = self._json_post(
            f"repos/{self.repository()}/issues/{_inbox_issue()}/comments", f"body={body}"
        )
        return _int(payload.get("id") if isinstance(payload, dict) else None, "comment id")

    def _json_post(self, path: str, *fields: str) -> object:
        # gh api 只把显式 -f/--field 当作请求体字段；缺少 -f 会把 body=... 当成多余位置参数。
        arguments = ["api", "--method", "POST", path]
        for field in fields:
            arguments += ["-f", field]
        return _json(self._run, arguments)

    def dispatch_wake(self) -> bool:
        result = self._run(
            [
                "api",
                "--method",
                "POST",
                f"repos/{self.repository()}/actions/workflows/landing-queue.yml/dispatches",
                "-f",
                "ref=main",
            ]
        )
        return result.returncode == 0


def _inbox_issue() -> int:
    value = os.environ.get(INBOX_ISSUE_ENV)
    if value is None or not value.isdigit() or int(value) <= 0:
        raise InfrastructureError(f"{INBOX_ISSUE_ENV} must name the landing-queue inbox issue")
    return int(value)


def _env(name: str, error: type[QueueError]) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise error(f"{name} must be configured")
    return value.strip()


def _app_id() -> str:
    return _env(APP_ID_ENV, ProtectionError)


def _app_bot() -> str:
    return _env(APP_BOT_ENV, ProtectionError)


def _missing_source(message: str) -> bool:
    lowered = message.lower()
    return "404" in lowered or "not found" in lowered


def _refresh_failure_is_known(message: str) -> bool:
    """只有明确的客户端拒绝（4xx）或本地前置校验失败才能证明写未发生；
    5xx 变更响应的结果不确定，必须按模糊写处理并保持 REFRESHING。"""
    if re.search(r"\bHTTP\s+4\d\d\b", message, re.IGNORECASE):
        return True
    known = (
        "does not need a base refresh",
        "merge conflicts",
        "head changed",
        "local PR candidate",
        "not open",
        "base is not main",
        "draft pull request",
    )
    return any(fragment in message for fragment in known)


# --------------------------------------------------------------------------- 保护校验（C）


def _applies(rule: dict[str, Any], ref: str) -> bool:
    """只接受显式、精确、无排除的分支范围；通配/~ALL/排除/未知结构一律不生效。"""
    if rule.get("enforcement") != "active" or rule.get("target") != "branch":
        return False
    conditions = rule.get("conditions")
    refs = conditions.get("ref_name") if isinstance(conditions, dict) else None
    if not isinstance(refs, dict):
        return False
    include = refs.get("include")
    exclude = refs.get("exclude")
    if not isinstance(include, list) or exclude:
        return False
    return include == [ref]


def _rule(rule: dict[str, Any], kind: str) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in rule.get("rules", [])
            if isinstance(item, dict) and item.get("type") == kind
        ),
        None,
    )


def _bypass_app_only(rule: dict[str, Any], app_id: str) -> bool:
    actors = rule.get("bypass_actors") or []
    return (
        len(actors) == 1
        and actors[0].get("actor_type") == "Integration"
        and str(actors[0].get("actor_id")) == app_id
        and actors[0].get("bypass_mode") == "always"
    )


def verify_protections(rulesets: list[dict[str, Any]], app_id: str) -> None:
    """读取实际生效的 rulesets；缺失/禁用/错误目标/通配/排除/外部 bypass 一律 fail closed。

    合同固定为三个生效的分支 ruleset：无 bypass 的 main 佐证规则（squash、
    resolved threads、strict 检查且 `CI required`/`Landing gate` 绑定 App）、
    仅 App 可绕过的 main update 限制，以及仅 App 可绕过的状态分支 update 限制。
    任何额外覆盖 main/状态分支的生效规则都 fail closed，不自行实现 GitHub 通配引擎。
    """
    main_rules = [rule for rule in rulesets if _applies(rule, "refs/heads/main")]
    state_rules = [rule for rule in rulesets if _applies(rule, f"refs/heads/{STATE_BRANCH}")]

    evidence_candidates = [
        rule
        for rule in main_rules
        if _rule(rule, "pull_request") and _rule(rule, "required_status_checks")
    ]
    if len(evidence_candidates) != 1:
        raise ProtectionError("exactly one main evidence ruleset is required")
    evidence = evidence_candidates[0]
    if evidence.get("bypass_actors"):
        raise ProtectionError("the main evidence ruleset must not bypass any actor")
    pull = _rule(evidence, "pull_request")["parameters"]
    if pull.get("allowed_merge_methods") != ["squash"]:
        raise ProtectionError("evidence ruleset must allow squash merge only")
    if pull.get("required_review_thread_resolution") is not True:
        raise ProtectionError("evidence ruleset must require resolved review threads")
    checks = _rule(evidence, "required_status_checks")["parameters"]
    if checks.get("strict_required_status_checks_policy") is not True:
        raise ProtectionError("evidence ruleset must require strict up-to-date checks")
    contexts = {
        item.get("context"): str(item.get("integration_id"))
        for item in checks.get("required_status_checks") or []
        if isinstance(item, dict)
    }
    for context in ("CI required", "Landing gate"):
        if contexts.get(context) != app_id:
            raise ProtectionError(f"{context} must be bound to the landing App integration id")

    main_update = [
        rule
        for rule in main_rules
        if _rule(rule, "update")
        and not _rule(rule, "pull_request")
        and not _rule(rule, "required_status_checks")
    ]
    if len(main_update) != 1 or not _bypass_app_only(main_update[0], app_id):
        raise ProtectionError("main update restriction must bypass exactly the landing App")

    state_update = [rule for rule in state_rules if _rule(rule, "update")]
    if len(state_update) != 1 or not _bypass_app_only(state_update[0], app_id):
        raise ProtectionError("queue-state update restriction must bypass exactly the landing App")

    accounted = {id(evidence), id(main_update[0]), id(state_update[0])}
    for rule in rulesets:
        if id(rule) in accounted:
            continue
        if _applies(rule, "refs/heads/main") or _applies(rule, f"refs/heads/{STATE_BRANCH}"):
            raise ProtectionError(
                "an unexpected active ruleset also applies to main or the queue state branch"
            )


# --------------------------------------------------------------------------- 编排器


def _wake_next(backend: Backend, state: State) -> None:
    if any(entry.state in {QUEUED, ACTIVE} for entry in state.entries):
        backend.dispatch_wake()


def _cas(backend: Backend, state: State, revision: str) -> None:
    if not backend.write_state(state, revision):
        raise InfrastructureError("queue state CAS conflicted; another writer is active")


def _revoke_gates(backend: Backend, entry: Entry) -> None:
    """释放所有权前撤销可能存在的成功门禁，避免旧成功在状态变更后存活。"""
    if not entry.ci_head:
        return
    backend.publish_status(entry.ci_head, "CI required", "failure", "landing entry released")
    backend.publish_status(entry.ci_head, "Landing gate", "failure", "landing entry released")


def _block_evidence(
    backend: Backend,
    current: Entry,
    *,
    observed: str | None = None,
    main: str | None = None,
    run: str | None = None,
    attempt: str | None = None,
) -> dict[str, str]:
    """每次阻塞前保存完整的不透明证据；block() 再补 reason/category。"""
    return {
        "handoff": current.handoff,
        "expected_head": current.ci_head or current.candidate,
        "observed_head": observed
        if observed is not None
        else backend.pull_request(current.pr).head_oid,
        "main": main if main is not None else backend.main_sha(),
        # 未开始的运行用显式 "none" 哨兵：解码端禁止空字符串，durable JSON 必须可读。
        "run": (run or current.ci_run_id) or "none",
        "attempt": (attempt or current.ci_attempt) or "none",
    }


def _release_transition(
    backend: Backend, before: Entry | None, after: Entry | None
) -> Entry | None:
    """消费改变所有权时撤销旧 ACTIVE 门禁并补齐阻塞证据；MERGING 不可变。"""
    if (
        before is not None
        and before.state == ACTIVE
        and before.phase != MERGING
        and (after is None or after.state != ACTIVE)
    ):
        _revoke_gates(backend, before)
    if after is not None and after.state == BLOCKED:
        evidence = after.evidence or {}
        if not set(EVIDENCE_KEYS).issubset(evidence):
            base = before or after
            return replace(after, evidence={**_block_evidence(backend, base), **evidence})
    return None


def _trusted(backend: Backend, comment: Comment) -> bool:
    return comment_is_trusted(comment, backend.permission(comment.author))


def _enqueue_preconditions(
    pr: land_pr.PullRequestState, repository: str, request: Request, attestation: Attestation
) -> bool:
    return (
        pr.state == "OPEN"
        and pr.base_name == "main"
        and not pr.draft
        and pr.head_oid == request.candidate
        and pr.head_repository in {"", repository}
        and attestation.root == request.candidate
        and attestation.pr == request.pr
        and attestation.head_branch == pr.head_name
    )


def _consume(backend: Backend, state: State) -> State:
    """按服务器 id 消费新评论；非法/外来评论跳过，不得接管或卡死有效队列。"""
    comments = sorted(backend.inbox(), key=lambda comment: comment.id)
    fresh = [comment for comment in comments if comment.id > state.consumed_request_id]
    if not fresh:
        return state
    repository = backend.repository()
    result = state
    highest = state.consumed_request_id
    for comment in fresh:
        highest = max(highest, comment.id)
        if not _trusted(backend, comment):
            continue
        try:
            request = request_from_comment(comment)
        except QueueError:
            continue
        if request is None:
            continue
        try:
            if request.kind in REPAIR_KINDS:
                result = apply_repair(result, request)
                continue
            before = find(result, request.pr)
            if request.kind == "enqueue":
                attestation_comment = backend.comment(request.attestation_comment)
                attestation = attestation_from_comment(attestation_comment, repository, request.pr)
                if attestation.digest != request.attestation_digest or not _trusted(
                    backend, attestation_comment
                ):
                    continue
                if not _enqueue_preconditions(
                    backend.pull_request(request.pr), repository, request, attestation
                ):
                    continue
                updated = apply_enqueue(result, request, attestation)
            else:
                updated = apply_dequeue(result, request)
            after = find(updated, request.pr)
            replacement = _release_transition(backend, before, after)
            if replacement is not None:
                updated = _replace_entry(updated, request.pr, evidence=replacement.evidence)
            result = updated
        except InfrastructureError:
            raise
        except QueueError:
            continue
    return replace(result, consumed_request_id=highest)


def enqueue(
    backend: Backend,
    number: int,
    expected_head: str,
    attestation_comment: int,
    *,
    repair_claim: str = "",
) -> int:
    repository = backend.repository()
    pr = backend.pull_request(number)
    _require_open_main(pr)
    if pr.head_oid != expected_head:
        raise QueueError("pull-request head does not match --expected-head")
    if pr.head_repository not in {"", repository}:
        raise QueueError("fork pull requests cannot enter landing")
    local_candidate(pr, expected_head)
    comment = backend.comment(attestation_comment)
    attestation = attestation_from_comment(comment, repository, number)
    if not _trusted(backend, comment):
        raise QueueError("attestation comment is not from an unedited human maintainer")
    if attestation.root != expected_head or attestation.head_branch != pr.head_name:
        raise QueueError("attestation does not match this pull request and root")
    state, _ = backend.state()
    existing = find(state, number)
    if (
        existing is not None
        and existing.state == ACTIVE
        and existing.authorization_root != expected_head
    ):
        raise QueueError(f"pr {number} is ACTIVE with a different authorization root")
    repair = _repair_state(existing) if existing is not None and existing.state == BLOCKED else None
    digest = claim_digest(repair_claim) if repair_claim else ""
    if repair in REPAIR_STATES:
        claim = (existing.evidence or {}).get("repair_claim")
        if repair not in REPAIR_TERMINAL_STATES or digest != claim:
            # 与控制器保持同一契约：claimed/resuming 一律阻止重入队，终态才接受匹配 token。
            raise QueueError("re-enqueue requires a terminal repair claim and its matching token")
    handoff = secrets.token_hex(16)
    backend.post_request(
        request_body(
            Request(
                "enqueue",
                number,
                0,
                expected_head,
                handoff,
                attestation.comment,
                attestation.digest,
                claim=digest,
            )
        )
    )
    local_receipt(handoff, number, expected_head, pr.head_name)
    dispatched = backend.dispatch_wake()
    # 新的候选在旧根 QUEUED 时不得报告为“已接受转移”；控制器会以 BLOCKED_MUTATION 处理。
    queued = (
        existing is not None
        and existing.state == QUEUED
        and existing.authorization_root != expected_head
    )
    outcome = "requested-blocked-mutation" if queued else "enqueued"
    print(f"state={outcome}\npr={number}\nroot={expected_head}\nhandoff={handoff}")
    print(f"dispatch={'sent' if dispatched else 'failed'}")
    return 0


def dequeue(backend: Backend, number: int) -> int:
    backend.post_request(request_body(Request("dequeue", number, 0)))
    dispatched = backend.dispatch_wake()
    # 控制器尚未处理前，所有权仍属于当前写入者；这里只报告“已请求”。
    print(f"state=dequeue-requested\npr={number}")
    print(f"dispatch={'sent' if dispatched else 'failed'}")
    return 0


def queue_status(backend: Backend) -> int:
    state, _ = backend.state()
    current = active(state)
    active_pr = current.pr if current else "none"
    print(f"consumed_request_id={state.consumed_request_id}\nactive={active_pr}")
    for entry in ordered(state):
        phase = entry.phase or "none"
        blocked = entry.blocked_reason or "none"
        print(f"entry pr={entry.pr} state={entry.state} phase={phase} blocked={blocked}")
    return 0


def prepare(
    backend: Backend, run_id: str, attempt: str, controller: str, event: RefreshEvent | None
) -> int:
    """消费请求并推进 ACTIVE 阶段；在触发可复用 CI 前持久化 TESTING intent。"""
    verify_protections(backend.rulesets(), _app_id())
    state, revision = backend.state()
    consumed = _consume(backend, state)
    if consumed != state:
        _cas(backend, consumed, revision)
        state, revision = backend.state()
    else:
        state = consumed
    current = active(state)
    if current is None:
        print("run_ci=false\nactive=none")
        return 0
    repository = backend.repository()
    pr = backend.pull_request(current.pr)
    if current.phase == MERGING:
        return _prove_or_recover(backend, state, revision, current)
    if current.phase == REFRESHING:
        if pr.head_oid == current.candidate:
            # 头未变仍 BEHIND：不得发起第二次 refresh，也不得 CI。
            print(f"run_ci=false\nactive={current.pr}\nphase=REFRESHING")
            return 0
        return _resolve_refresh(
            backend, state, revision, current, pr, event, run_id, attempt, controller
        )
    if current.phase == TESTING:
        return _testing_release(backend, state, revision, current)
    # 任何新的集成动作（refresh/CI）之前重读佐证；撤销/编辑/降权即 fail closed 释放。
    try:
        _require_authority(backend, repository, current, pr)
    except ProtectionError:
        return _block_and_report(backend, state, revision, current, BLOCKED_AUTHORITY, pr)
    if pr.head_oid != current.candidate:
        return _block_and_report(backend, state, revision, current, BLOCKED_MUTATION, pr)
    if pr.merge_state == "DIRTY" or pr.mergeable == "CONFLICTING":
        return _block_and_report(backend, state, revision, current, BLOCKED_CONFLICT, pr)
    if pr.merge_state == "BEHIND":
        intent = begin_refresh(state, current.pr, current.candidate, backend.main_sha())
        _cas(backend, intent, revision)
        # 副作用前已持久化 REFRESHING intent；重读以在已知失败时用最新 revision 释放。
        state, revision = backend.state()
        current = find(state, current.pr)
        try:
            backend.refresh(current.pr, current.candidate)
        except land_pr.LandingError as exc:
            if _refresh_failure_is_known(str(exc)):
                return _block_and_report(backend, state, revision, current, BLOCKED_MUTATION, pr)
            # 无法证明写入未发生：保留 REFRESHING intent，要求运维显式唤醒，不做猜测式重试。
            raise InfrastructureError(
                "refresh write outcome is ambiguous; holding the REFRESHING intent until an "
                f"operator wakes the queue: {exc}"
            ) from exc
        print(f"run_ci=false\nactive={current.pr}\nphase=REFRESHING")
        return 0
    latest = backend.main_sha()
    if pr.base_oid != latest:
        return _block_and_report(backend, state, revision, current, BLOCKED_SCOPE, pr)
    intent = begin_testing(
        state, current.pr, run_id, attempt, controller, current.candidate, latest
    )
    if intent != state:
        _cas(backend, intent, revision)
    print(f"run_ci=true\nactive={current.pr}\nhead={current.candidate}\nbase={latest}")
    return 0


def _resolve_refresh(
    backend: Backend,
    state: State,
    revision: str,
    current: Entry,
    pr: land_pr.PullRequestState,
    event: RefreshEvent | None,
    run_id: str,
    attempt: str,
    controller: str,
) -> int:
    """仅在 parent 关系与匹配 App 事件都证明受控集成时冻结新头并持久化 TESTING；
    无 App 事件时保持 REFRESHING 等待，真实 mutation/伪造事件 fail closed。"""
    root, base = current.refresh_root, current.refresh_base
    if root is None or base is None:
        raise InfrastructureError("REFRESHING intent is missing its root/base")
    if backend.main_sha() != base:
        return _block_and_report(backend, state, revision, current, BLOCKED_SCOPE, pr)
    try:
        _require_authority(backend, backend.repository(), current, pr)
    except ProtectionError:
        return _block_and_report(backend, state, revision, current, BLOCKED_AUTHORITY, pr)
    parents = backend.commit_parents(pr.head_oid)
    if tuple(parents) != (root, base):
        # 真实头变化不能由无事件早返回掩盖：parent 关系异常一律 fail closed。
        return _block_and_report(backend, state, revision, current, BLOCKED_MUTATION, pr)
    if event is None:
        # 普通 wake（workflow_dispatch 等）没有 App synchronize 佐证：保持 REFRESHING intent 与状态
        # 不变，不继承授权、不触发 CI、不发起第二次 refresh，等待匹配的 App 事件。
        print(f"run_ci=false\nactive={current.pr}\nphase=REFRESHING")
        return 0
    # 显式事件必须同时匹配 PR 号、精确旧/新头与 App actor；同一 SHA 的其它 PR 事件不能代签。
    event_ok = (
        event.pr == current.pr
        and event.before == root
        and event.after == pr.head_oid
        and event.actor == _app_bot()
    )
    if not event_ok:
        # 伪造或并发替换的事件：不继承授权，也不 CI 错误的树。
        return _block_and_report(backend, state, revision, current, BLOCKED_MUTATION, pr)
    accepted = accept_refresh(state, current.pr, pr.head_oid, base)
    intent = begin_testing(accepted, current.pr, run_id, attempt, controller, pr.head_oid, base)
    _cas(backend, intent, revision)
    print(f"run_ci=true\nactive={current.pr}\nhead={pr.head_oid}\nbase={base}")
    return 0


def _testing_release(backend: Backend, state: State, revision: str, current: Entry) -> int:
    """重复事件命中 TESTING：绝不重跑 CI；上一次运行已结束则释放为基础设施。"""
    run = backend.run(current.ci_run_id or "")
    if str(run.get("status") or "") in IN_FLIGHT:
        print(f"run_ci=false\nactive={current.pr}\nphase=TESTING")
        return 0
    evidence = _block_evidence(backend, current, run=current.ci_run_id)
    _revoke_gates(backend, current)
    blocked = block(state, current.pr, BLOCKED_INFRA, evidence)
    _cas(backend, blocked, revision)
    _wake_next(backend, blocked)
    print(f"run_ci=false\nblocked={BLOCKED_INFRA}")
    return 0


def _block_and_report(
    backend: Backend,
    state: State,
    revision: str,
    current: Entry,
    reason: str,
    pr: land_pr.PullRequestState,
) -> int:
    evidence = _block_evidence(backend, current, observed=pr.head_oid, main=backend.main_sha())
    _revoke_gates(backend, current)
    blocked = block(state, current.pr, reason, evidence)
    _cas(backend, blocked, revision)
    _wake_next(backend, blocked)
    print(f"run_ci=false\nblocked={reason}")
    return 0


def _references_trusted_ci(run: dict[str, Any], controller: str, repository: str) -> bool:
    """证明 pull_request_target 运行引用的可复用 CI 来自同仓库固定 main。

    GitHub 上同一次运行有三个 SHA 身份：run.head_sha/head_branch 是触发 PR 的头，
    github.workflow_sha（= ci_controller）是实际执行的 workflow 源码提交。PR-target
    运行执行默认分支代码，但头属于触发 PR（可能不是 ACTIVE 候选），因此头字段不能证明
    源码来源；只信 referenced_workflows 中同仓库、固定 refs/heads/main、精确 controller
    提交的 blocking-ci.yml。缺失、非 list、条目非 dict、外来仓库或其它路径/ref/sha 一律
    fail closed，绝不把 AttributeError/畸形元数据当作成功。
    """
    workflows = run.get("referenced_workflows")
    if not isinstance(workflows, list):
        return False
    expected = f"{repository}/{REUSABLE_CI_PATH}@{controller}"
    for item in workflows:
        if not isinstance(item, dict):
            return False
        if (
            item.get("sha") == controller
            and item.get("ref") == "refs/heads/main"
            and item.get("path") == expected
        ):
            return True
    return False


def _run_matches(run: dict[str, Any], current: Entry, repository: str) -> bool:
    repo = run.get("repository")
    if (
        run.get("path") != CONTROLLER_PATH
        or run.get("event") not in TRUSTED_EVENTS
        or not isinstance(repo, dict)
        or repo.get("full_name") != repository
        or str(run.get("run_attempt")) != str(current.ci_attempt)
    ):
        return False
    if run.get("event") == "pull_request_target":
        # 触发头不是 CI 头，也不能绑定当前 ci_head：PR2 事件可唤醒当前队列 PR1。
        return _references_trusted_ci(run, current.ci_controller or "", repository)
    # 其它可信事件运行在 main 上，头即受信 controller 提交。
    return run.get("head_sha") == current.ci_controller and run.get("head_branch") == "main"


CALLER_JOB_PREFIX = "Single-flight blocking CI / "


def _job_base_name(name: str) -> str:
    """去掉可复用工作流的调用者前缀与 matrix 后缀，得到 blocking-ci 内部作业名。"""
    base = name[len(CALLER_JOB_PREFIX) :] if name.startswith(CALLER_JOB_PREFIX) else name
    match = re.fullmatch(r"(.+?) \([^()]+\)", base)
    return match.group(1) if match is not None else base


def _run_category(backend: Backend, run_id: str) -> str:
    """只根据真实的可复用 CI 作业分类；控制器自身/未选中/未完成的作业不参与。"""
    terminal = []
    for job in backend.jobs(run_id):
        if _job_base_name(str(job.get("name") or "")) not in REUSABLE_CI_JOB_NAMES:
            continue
        if str(job.get("status") or "") != "completed":
            continue
        if str(job.get("conclusion") or "") in {"skipped", "neutral", ""}:
            continue
        terminal.append(job)
    if not terminal:
        return "infrastructure"
    conclusions = {str(job.get("conclusion")) for job in terminal}
    if conclusions & INFRA_CONCLUSIONS:
        return "infrastructure"
    if conclusions <= {"failure", "success"} and "failure" in conclusions:
        return "candidate"
    return "infrastructure"


def finalize(backend: Backend, number: int, run_id: str, attempt: str, ci_result: str) -> int:
    repository = backend.repository()
    # 服务器保护缺失：在任何 status/分支/状态写入之前 fail closed。
    verify_protections(backend.rulesets(), _app_id())
    state, revision = backend.state()
    consumed = _consume(backend, state)
    if consumed != state:
        _cas(backend, consumed, revision)
        state, revision = backend.state()
    else:
        state = consumed
    current = find(state, number)
    if current is None or current.state != ACTIVE:
        # dequeue 在 _consume 中已经撤销门禁；这里只干净地 no-op，不重复发布状态。
        print("finalize=released")
        return 0
    if current.phase == MERGING:
        return _prove_or_recover(backend, state, revision, current)
    if current.phase != TESTING:
        raise QueueError("finalize requires a TESTING phase")
    if (current.ci_run_id, current.ci_attempt) != (run_id, attempt):
        raise QueueError("finalize run identity does not match the recorded CI intent")
    run = backend.run(run_id)
    if not _run_matches(run, current, repository):
        raise QueueError("Actions run identity does not match the recorded controller intent")
    if ci_result in {"cancelled", "timed_out", "startup_failure", "skipped"}:
        return _finalize_block(backend, state, revision, current, BLOCKED_INFRA, run_id)
    if ci_result != "success":
        reason = BLOCKED_INFRA if _run_category(backend, run_id) == "infrastructure" else BLOCKED_CI
        return _finalize_block(backend, state, revision, current, reason, run_id)
    pr = backend.pull_request(number)
    if pr.head_oid != current.ci_head:
        return _finalize_block(backend, state, revision, current, BLOCKED_MUTATION, run_id)
    latest = backend.main_sha()
    if latest != current.ci_base:
        # main 在 CI 期间移动：撤销旧成功门禁并以 BLOCKED_SCOPE 释放。
        return _finalize_block(backend, state, revision, current, BLOCKED_SCOPE, run_id, pr)
    if str(pr.review_decision).lower() == "changes_requested":
        return _finalize_block(backend, state, revision, current, BLOCKED_REVIEW, run_id)
    if pr.merge_state == "DIRTY" or pr.mergeable == "CONFLICTING":
        return _finalize_block(backend, state, revision, current, BLOCKED_CONFLICT, run_id)
    try:
        _require_authority(backend, repository, current, pr)
    except ProtectionError:
        return _finalize_block(backend, state, revision, current, BLOCKED_AUTHORITY, run_id)
    backend.publish_status(current.ci_head, "CI required", "success", "single-flight blocking CI")
    backend.publish_status(current.ci_head, "Landing gate", "success", "landing queue authority")
    intent = begin_merge(state, number, current.ci_head)
    _cas(backend, intent, revision)
    state, revision = backend.state()
    return _merge_and_prove(backend, state, revision, find(state, number))


def _finalize_block(
    backend: Backend,
    state: State,
    revision: str,
    current: Entry,
    reason: str,
    run_id: str,
    pr: land_pr.PullRequestState | None = None,
) -> int:
    observed = pr.head_oid if pr is not None else backend.pull_request(current.pr).head_oid
    evidence = _block_evidence(
        backend, current, observed=observed, main=backend.main_sha(), run=run_id
    )
    _revoke_gates(backend, current)
    blocked = block(state, current.pr, reason, evidence)
    _cas(backend, blocked, revision)
    _wake_next(backend, blocked)
    print(f"finalize=blocked reason={reason}")
    return 0


def _merge_and_prove(backend: Backend, state: State, revision: str, current: Entry) -> int:
    try:
        merge_commit = backend.merge(current.pr, current.candidate)
    except land_pr.LandingError as exc:
        _revoke_gates(backend, current)
        raise InfrastructureError(f"merge seam did not complete: {exc}") from exc
    return _prove(backend, state, revision, current, merge_commit)


def _prove_or_recover(backend: Backend, state: State, revision: str, current: Entry) -> int:
    """崩溃恢复：用已合并 PR 证明结果，不重跑 CI、不重复合并。"""
    pr = backend.pull_request(current.pr)
    if pr.state != "MERGED" or pr.merge_commit is None:
        return _block_and_report(backend, state, revision, current, BLOCKED_MUTATION, pr)
    return _prove(backend, state, revision, current, pr.merge_commit)


def _prove(backend: Backend, state: State, revision: str, current: Entry, merge_commit: str) -> int:
    """合并证明：PR 身份、squash 在 main 祖先链、被测头树 == 合并树。"""
    repository = backend.repository()
    pr = backend.pull_request(current.pr)
    tested = current.ci_head or current.candidate
    if (
        pr.state != "MERGED"
        or pr.merge_commit != merge_commit
        or pr.head_oid != tested
        or pr.base_name != "main"
    ):
        raise InfrastructureError("merged pull request does not match the tested candidate")
    _require_authority(backend, repository, current, pr)
    if not backend.is_ancestor(merge_commit, backend.main_sha()):
        raise InfrastructureError("merge commit is not retained on main")
    tested_tree = backend.tree_sha(tested)
    if tested_tree != backend.tree_sha(merge_commit):
        raise InfrastructureError("tested head tree does not match the merged tree")
    completed = complete(state, current.pr, merge_commit, tested_tree)
    _cas(backend, completed, revision)
    _wake_next(backend, completed)
    print(f"finalize=merged merge_commit={merge_commit}")
    return 0


def verify_main(backend: Backend, pushed: str) -> int:
    """push main 的轻量完整性：必须证明该提交就是已记录的合并证明。"""
    state, _ = backend.state()
    recorded = [entry for entry in state.entries if entry.merge_commit]
    if not recorded:
        # 空初始化：显式标记 bootstrap pending，绝不假装已验证合并证明。
        print("integrity=bootstrap-pending")
        return 0
    for entry in recorded:
        if entry.merge_commit == pushed and backend.tree_sha(pushed) == entry.merge_tree:
            print(f"integrity=verified pr={entry.pr}")
            return 0
    raise InfrastructureError(f"pushed commit {pushed} is not a recorded merge proof")


def _require_authority(
    backend: Backend, repository: str, current: Entry, pr: land_pr.PullRequestState
) -> None:
    """每个集成动作都重读佐证：删除/编辑/降权/换根/损坏即失效。"""
    try:
        comment = backend.comment(current.attestation_comment)
    except (MissingSourceError, InfrastructureError) as exc:
        # 404 是“来源已删除”的确定事实；其它错误是 API 传输故障，按基础设施抛出。
        if isinstance(exc, MissingSourceError) or _missing_source(str(exc)):
            raise ProtectionError("attestation comment no longer exists") from exc
        raise
    try:
        attestation = attestation_from_comment(comment, repository, current.pr)
    except InfrastructureError:
        raise
    except QueueError as exc:
        # 结构损坏是确定性的授权失效，不是传输故障。
        raise ProtectionError(f"attestation is no longer parseable: {exc}") from exc
    if not _trusted(backend, comment):
        raise ProtectionError("attestation is no longer an unedited human maintainer comment")
    if attestation.digest != current.attestation_digest:
        raise ProtectionError("attestation digest changed; authority is revoked")
    if (
        attestation.root != current.authorization_root
        or attestation.author != current.attestation_author
    ):
        raise ProtectionError("attestation root or author no longer matches")
    if attestation.head_branch != pr.head_name or not comment.issue_url.endswith(
        f"/issues/{current.pr}"
    ):
        raise ProtectionError("attestation does not match this pull request")


def _require_open_main(pr: land_pr.PullRequestState) -> None:
    if pr.state != "OPEN":
        raise QueueError(f"pull request is not open: {pr.state.lower()}")
    if pr.base_name != "main":
        raise QueueError(f"pull request base is not main: {pr.base_name}")
    if pr.draft:
        raise QueueError("draft pull request cannot enter landing")


# --------------------------------------------------------------------------- 本地校验


def local_candidate(pr: land_pr.PullRequestState, expected_head: str) -> None:
    try:
        root = current_worktree()
        branch = current_branch()
        binding = load_binding(binding_path(root))
    except BindingError as exc:
        raise QueueError(f"task worktree is not bound: {exc}") from exc
    if binding["branch"] != branch or canonical_worktree(str(binding["worktree"])) != root:
        raise QueueError("session binding does not match this worktree and branch")
    if branch != pr.head_name:
        raise QueueError(f"local branch {branch} is not the pull-request head {pr.head_name}")
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0 or result.stdout.strip() != expected_head:
        raise QueueError("local HEAD is not the frozen candidate")
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode != 0 or status.stdout.strip():
        raise QueueError("task worktree is not clean")


def local_receipt(handoff: str, pr: int, root: str, branch: str) -> Path:
    directory = Path(".nvsop") / "artifacts" / "landing"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{handoff}.json"
    path.write_text(
        json.dumps({"handoff": handoff, "pr": pr, "root": root, "branch": branch}, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------- CLI


def _event() -> RefreshEvent | None:
    before, after, actor, number = (
        os.environ.get(name) for name in ("EVENT_BEFORE", "EVENT_AFTER", "EVENT_ACTOR", "EVENT_PR")
    )
    if not (before and after and actor and number and number.isdigit()):
        return None
    return RefreshEvent(before, after, actor, int(number))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Automatic PR landing queue.")
    commands = result.add_subparsers(dest="command", required=True)
    enqueue_command = commands.add_parser("enqueue", help="Post one durable enqueue request.")
    enqueue_command.add_argument("--pr", required=True, type=int)
    enqueue_command.add_argument("--expected-head", required=True)
    enqueue_command.add_argument("--attestation", required=True, type=int)
    enqueue_command.add_argument("--repair-claim", default="")
    dequeue_command = commands.add_parser("dequeue", help="Post one durable dequeue request.")
    dequeue_command.add_argument("--pr", required=True, type=int)
    commands.add_parser("queue", help="Read the current queue state.")
    repair_command = commands.add_parser(
        "repair-event", help="Print one machine-readable blocked-entry repair event."
    )
    repair_command.add_argument("--pr", required=True, type=int)
    prepare_command = commands.add_parser(
        "prepare", help="Consume requests and drive the ACTIVE phase."
    )
    prepare_command.add_argument("--run-id", required=True)
    prepare_command.add_argument("--attempt", required=True)
    prepare_command.add_argument("--controller-sha", required=True)
    verify_command = commands.add_parser(
        "verify-main", help="Verify recorded merge proof on a pushed main commit."
    )
    verify_command.add_argument("--sha", required=True)
    finalize_command = commands.add_parser(
        "finalize", help="Publish gates and merge the ACTIVE entry."
    )
    finalize_command.add_argument("--pr", required=True, type=int)
    finalize_command.add_argument("--run-id", required=True)
    finalize_command.add_argument("--attempt", required=True)
    finalize_command.add_argument("--ci-result", required=True)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    backend: Backend = GitHub()
    try:
        if arguments.command == "enqueue":
            return enqueue(
                backend,
                arguments.pr,
                arguments.expected_head,
                arguments.attestation,
                repair_claim=arguments.repair_claim,
            )
        if arguments.command == "dequeue":
            return dequeue(backend, arguments.pr)
        if arguments.command == "queue":
            return queue_status(backend)
        if arguments.command == "repair-event":
            print(json.dumps(repair_event(backend, arguments.pr), sort_keys=True))
            return 0
        if arguments.command == "prepare":
            return prepare(
                backend, arguments.run_id, arguments.attempt, arguments.controller_sha, _event()
            )
        if arguments.command == "finalize":
            return finalize(
                backend, arguments.pr, arguments.run_id, arguments.attempt, arguments.ci_result
            )
        if arguments.command == "verify-main":
            return verify_main(backend, arguments.sha)
        raise AssertionError(f"unexpected command: {arguments.command}")
    except QueueError as exc:
        print(f"landing_queue: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
