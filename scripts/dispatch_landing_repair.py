#!/usr/bin/env python3
"""把 BLOCKED 落地条目派发给其绑定的实现会话；无可证明 host 桥即 fail closed。

公开事件不含 session 身份/worktree 路径；私有 resume handoff 才携带。声明经可信控制器
CAS inbox 独占接受、推进 claimed→resuming 后才调 host，故重入队无法制造第二写入者。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))

import landing_queue as lq
from bind_task_session import BindingError, binding_path, canonical_worktree, load_binding
from cleanup_task import Worktree, parse_worktrees

OWNERSHIP, NEXT_ACTION = "DEVELOPMENT", "repair"
# origin URL（https/ssh/scp 形式）纯本地解析 owner/repo；不调用 gh。
ORIGIN = re.compile(
    r"(?:git@[^:]+:|(?:https?|ssh)://(?:[^/@]+@)?[^/]+/)"
    r"(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?"
)


class DispatchError(RuntimeError):
    """派发前置条件不满足，或请求无法安全完成。"""


class AgentUnavailableError(DispatchError):
    """原实现会话无法被证明可恢复；无替代并保留 BLOCKED 队列。"""


@dataclass(frozen=True)
class ResumeRequest:
    """交给 HostBridge 的私有恢复请求；含 session 身份，绝不进入公开状态。"""

    session_id: str
    handoff: Mapping[str, object]


class ResumeOutcome(StrEnum):
    RESUMED = "resumed"
    UNAVAILABLE = "unavailable"


class HostBridge(Protocol):
    """provider-neutral host seam：证明 exact session 可恢复且旧写入者已停止。"""

    def verify(self, request: ResumeRequest) -> bool: ...

    def resume(self, request: ResumeRequest) -> ResumeOutcome: ...


class UnavailableHostBridge:
    """默认桥：本机没有可证明的 host 唤醒/会话空闲 seam，一律 fail closed。"""

    def verify(self, request: ResumeRequest) -> bool:
        return False

    def resume(self, request: ResumeRequest) -> ResumeOutcome:
        return ResumeOutcome.UNAVAILABLE


def _git(*arguments: str, cwd: str | None = None) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise DispatchError("git command failed: " + " ".join(arguments))
    return result.stdout.strip()


def registered_worktree(branch: str) -> str:
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain", "-z"], capture_output=True, check=False
    )
    if result.returncode != 0:
        raise DispatchError("cannot read git worktree registration")
    try:
        worktrees: list[Worktree] = parse_worktrees(result.stdout)
    except Exception as exc:
        raise DispatchError("git worktree registration is malformed") from exc
    paths = [item.path for item in worktrees if item.branch == f"refs/heads/{branch}"]
    if len(paths) != 1:
        raise AgentUnavailableError("task branch is not registered in exactly one worktree")
    return canonical_worktree(paths[0])


def require_repository(root: str, expected: str) -> None:
    match = ORIGIN.fullmatch(_git("remote", "get-url", "origin", cwd=root).strip())
    if match is None or f"{match.group('owner')}/{match.group('repo')}" != expected:
        raise DispatchError("task worktree origin does not match the repair event repository")


def resolve_target(event: Mapping[str, object]) -> tuple[str, str]:
    branch = str(event["branch"])
    root = registered_worktree(branch)
    try:
        binding = load_binding(binding_path(root))
    except BindingError as exc:
        raise AgentUnavailableError("task worktree has no valid session binding") from exc
    if binding["branch"] != branch or canonical_worktree(str(binding["worktree"])) != root:
        raise AgentUnavailableError("session binding does not match the registered task worktree")
    receipt = Path(root) / ".nvsop" / "artifacts" / "landing" / f"{event['handoff']}.json"
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentUnavailableError("local landing receipt is missing or unreadable") from exc
    keys = ("handoff", "pr", "root", "branch")
    expected = (event["handoff"], event["pr"], event["authorization_root"], branch)
    if not isinstance(payload, dict) or tuple(payload.get(k) for k in keys) != expected:
        raise AgentUnavailableError("local receipt does not match the repair event")
    return root, str(binding["session_id"])


def resume_handoff(
    event: Mapping[str, object], target: tuple[str, str], digest: str
) -> dict[str, object]:
    evidence = event["evidence"]
    handoff = {"version": 1, "event": event["event"], "handoff": event["handoff"]}
    handoff |= {"repository": event["repository"], "branch": event["branch"]}
    handoff |= {"worktree": target[0], "session_id": target[1], "claim": digest}
    handoff |= {"pr": event["pr"], "candidate": event["landing_head"]}
    handoff |= {"observed_head": evidence["observed_head"], "current_main": event["main"]}
    handoff |= {
        "blocked_reason": event["blocked_state"],
        "failure_evidence": dict(event["failure"]),
    }
    handoff |= {"ownership": OWNERSHIP, "expected_next_action": NEXT_ACTION}
    return handoff


def _post(backend: lq.Backend, kind: str, event: Mapping[str, object], digest: str) -> None:
    request = lq.Request(kind, int(event["pr"]), 0, handoff=str(event["handoff"]), claim=digest)
    backend.post_request(lq.request_body(replace(request, generation=str(event["event"]))))
    backend.dispatch_wake()


def _unavailable(backend: lq.Backend, event: Mapping[str, object], digest: str, reason: str) -> int:
    _post(backend, lq.REPAIR_UNAVAILABLE, event, digest)
    print(f"state=unavailable\npr={event['pr']}" + (f"\nreason={reason}" if reason else ""))
    return 0


def _release(backend: lq.Backend, pr: int, entry: lq.Entry, digest: str, reason: str) -> int:
    """事件已过期（head/main 漂移或证据变更）：释放未决声明为终态，不调 host。"""
    stale = {"pr": pr, "handoff": entry.handoff, "event": lq.repair_generation(entry)}
    return _unavailable(backend, stale, digest, reason)


def claim(backend: lq.Backend, pr: int) -> int:
    event = lq.repair_event(backend, pr)
    token = secrets.token_hex(16)
    _post(backend, lq.REPAIR_CLAIM, event, lq.claim_digest(token))
    print(f"state=pending\npr={pr}\nevent={event['event']}\nclaim={token}")
    return 0


def resume(backend: lq.Backend, pr: int, host: HostBridge, claim: str) -> int:
    if not claim:
        raise DispatchError("resume requires --claim <token> from the accepted claim")
    entry = lq.find(backend.state()[0], pr)
    if entry is None or entry.state != lq.BLOCKED:
        raise DispatchError("no blocked entry to resume")
    evidence = entry.evidence or {}
    repair_state = evidence.get("repair_state")
    digest = lq.claim_digest(claim)
    if repair_state in lq.REPAIR_TERMINAL_STATES:
        print(f"state=already-{repair_state}\npr={pr}")
        return 0
    if repair_state == "resuming":
        # 单飞：只有刚推进 claimed→resuming 的调用继续；再次进入一律拒绝，不调 host。
        print(f"state=in-progress\npr={pr}")
        return 1
    if repair_state != "claimed" or digest != evidence.get("repair_claim"):
        raise DispatchError("no accepted repair claim matching this token")
    try:
        event = lq.repair_event(backend, pr)
    except lq.QueueError as exc:
        return _release(backend, pr, entry, digest, str(exc))
    if event["event"] != evidence.get("repair_generation"):
        return _release(backend, pr, entry, digest, "stale-event")
    # 调 host 前先 CAS 推进 claimed→resuming；未确认前不得调用 host，避免重入队竞态。
    _post(backend, lq.REPAIR_RESUMING, event, digest)
    current = lq.find(backend.state()[0], pr)
    if ((current.evidence or {}).get("repair_state") if current else None) != "resuming":
        print(f"state=pending-resume\npr={pr}")
        return 0
    try:
        target = resolve_target(event)
    except AgentUnavailableError as exc:
        return _unavailable(backend, event, digest, str(exc))
    request = ResumeRequest(target[1], resume_handoff(event, target, digest))
    try:
        verified = host.verify(request)
    except Exception:
        print(f"state=uncertain\npr={pr}")
        return 1
    if not verified:
        return _unavailable(backend, event, digest, "")
    try:
        outcome = host.resume(request)
    except Exception:
        print(f"state=uncertain\npr={pr}")
        return 1
    kind = lq.REPAIR_RESUMED if outcome is ResumeOutcome.RESUMED else lq.REPAIR_UNAVAILABLE
    _post(backend, kind, event, digest)
    print(f"state={kind.removeprefix('repair-')}\npr={pr}")
    return 0


def preflight(backend: lq.Backend, pr: int, worktree: str, expected_generation: str = "") -> int:
    event = lq.repair_event(backend, pr)
    root = canonical_worktree(worktree)
    branch = str(event["branch"])
    if registered_worktree(branch) != root:
        raise DispatchError("current worktree is not the registered task worktree")
    require_repository(root, str(event["repository"]))
    if _git("rev-parse", "--abbrev-ref", "HEAD", cwd=root) != branch:
        raise DispatchError("current branch is not the repair event branch")
    if _git("rev-parse", "HEAD", cwd=root) != event["evidence"]["observed_head"]:  # type: ignore[index]
        raise DispatchError("HEAD is not the blocked observed head")
    if expected_generation and event["event"] != expected_generation:
        raise DispatchError("blocked generation changed since preflight was requested")
    resolve_target(event)
    print(f"preflight=ok\npr={pr}\nbranch={branch}\nevent={event['event']}")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Dispatch one blocked landing entry to its session."
    )
    commands = result.add_subparsers(dest="command", required=True)
    claim_command = commands.add_parser("claim", help="Post one exclusive repair-claim request.")
    resume_command = commands.add_parser("resume", help="Resume the accepted session claim.")
    preflight_command = commands.add_parser("preflight", help="Re-verify facts before any write.")
    for command in (claim_command, resume_command, preflight_command):
        command.add_argument("--pr", required=True, type=int)
    resume_command.add_argument("--claim", required=True)
    preflight_command.add_argument("--worktree", default="")
    preflight_command.add_argument("--expected-generation", default="")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    backend: lq.Backend = lq.GitHub()
    try:
        if arguments.command == "claim":
            return claim(backend, arguments.pr)
        if arguments.command == "resume":
            return resume(backend, arguments.pr, UnavailableHostBridge(), arguments.claim)
        worktree = arguments.worktree or os.getcwd()
        return preflight(backend, arguments.pr, worktree, arguments.expected_generation)
    except (lq.QueueError, DispatchError) as exc:
        print(f"dispatch_landing_repair: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
