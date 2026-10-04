#!/usr/bin/env python3
"""本地 BLOCKED repair dispatcher：不依赖 WebCodex，复用 landing queue 的 CAS 协议。"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import selectors
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dispatch_landing_repair as dr
import landing_queue as lq
from bind_task_session import canonical_worktree

CLAIM_TOKEN = re.compile(r"^[0-9a-f]{32}$")
DEFAULT_INTERVAL_SECONDS = 15.0
PI_ACK_TIMEOUT_SECONDS = 15.0


class ServiceError(RuntimeError):
    """本地 service 配置、状态或 adapter 契约错误。"""


class Adapter(Protocol):
    name: str

    def verify(self, request: dr.ResumeRequest) -> bool: ...

    def resume(self, request: dr.ResumeRequest) -> dr.ResumeOutcome: ...


@dataclass(frozen=True)
class PiInstall:
    command: Path
    node: Path
    package_root: Path


def _read_json_line(path: Path) -> dict[str, object]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            line = handle.readline()
        value = json.loads(line)
    except (OSError, json.JSONDecodeError) as exc:
        raise ServiceError(f"cannot read Pi session header: {path}") from exc
    if not isinstance(value, dict):
        raise ServiceError(f"Pi session header is not an object: {path}")
    return value


def _pi_processes() -> list[str]:
    """返回 live `pi` 进程 cwd；Pi 无 session lock，因此同 cwd 必须保守视为潜在写者。"""
    result: list[str] = []
    for item in Path("/proc").iterdir():
        if not item.name.isdigit():
            continue
        try:
            if (item / "comm").read_text(encoding="utf-8").strip() != "pi":
                continue
            cwd = os.path.realpath(os.readlink(item / "cwd"))
        except OSError:
            continue
        result.append(cwd)
    return result


def pi_session_writer_may_be_alive(
    header: Mapping[str, object], worktree: str, processes: Sequence[str]
) -> bool:
    """Pi 没有 session lock；同原 cwd/目标 worktree 的 live Pi 都视为可能写者。"""
    cwd = header.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        raise ServiceError("Pi session header has no cwd")
    protected_cwds = {canonical_worktree(cwd), canonical_worktree(worktree)}
    return any(canonical_worktree(process_cwd) in protected_cwds for process_cwd in processes)


def resolve_pi_install(command: str | None) -> PiInstall:
    candidates: list[Path] = []
    if command:
        candidates.append(Path(command).expanduser())
    else:
        found = shutil.which("pi")
        if found:
            candidates.append(Path(found))
        nvm = Path.home() / ".nvm" / "versions" / "node"
        if nvm.is_dir():
            candidates.extend(path for path in nvm.glob("*/bin/pi") if path.exists())
    unique = {os.path.realpath(path): path for path in candidates if path.exists()}
    if len(unique) != 1:
        raise ServiceError("Pi adapter needs one unambiguous pi executable; pass --pi-command")
    launcher = next(iter(unique.values())).expanduser().absolute()
    if not launcher.exists():
        raise ServiceError("Pi executable disappeared during resolution")
    real = Path(os.path.realpath(launcher))
    if real.name != "cli.js" or real.parent.name != "bundle" or real.parent.parent.name != "dist":
        raise ServiceError("pi executable does not resolve to the supported package layout")
    package_root = real.parents[2]
    sibling_node = launcher.parent / "node"
    node = sibling_node if sibling_node.exists() else Path(shutil.which("node") or "")
    if not node or not node.exists():
        raise ServiceError("cannot resolve the Node executable for Pi")
    return PiInstall(launcher, node.resolve(strict=True), package_root.resolve(strict=True))


class PiAdapter:
    name = "pi"

    def __init__(
        self,
        *,
        command: str | None = None,
        session_dir: str | None = None,
        helper: Path | None = None,
        processes: Callable[[], list[str]] | None = None,
    ) -> None:
        self.install = resolve_pi_install(command)
        configured = session_dir or os.environ.get("PI_CODING_AGENT_SESSION_DIR")
        if configured:
            self.session_dir = Path(configured).expanduser()
        else:
            agent_dir = Path(os.environ.get("PI_CODING_AGENT_DIR", Path.home() / ".pi" / "agent"))
            self.session_dir = agent_dir / "sessions"
        self.helper = helper or Path(__file__).with_name("pi_resume_session.mjs")
        self._processes = processes or _pi_processes
        self._sessions: dict[str, Path] = {}
        self._children: list[subprocess.Popen[bytes]] = []

    def _session(self, session_id: str) -> Path:
        cached = self._sessions.get(session_id)
        if cached is not None:
            return cached
        if not self.session_dir.is_dir():
            raise ServiceError(f"Pi session directory does not exist: {self.session_dir}")
        suffix = f"_{session_id}.jsonl"
        matches = [
            path for path in self.session_dir.rglob(f"*{suffix}") if path.name.endswith(suffix)
        ]
        if len(matches) != 1:
            raise ServiceError(
                f"expected exactly one Pi session file for {session_id}, found {len(matches)}"
            )
        header = _read_json_line(matches[0])
        if header.get("type") != "session" or header.get("id") != session_id:
            raise ServiceError("Pi session header does not match the bound session id")
        self._sessions[session_id] = matches[0].resolve(strict=True)
        return self._sessions[session_id]

    def _probe(self, request: dr.ResumeRequest) -> bool:
        session = self._session(request.session_id)
        header = _read_json_line(session)
        handoff = request.handoff
        worktree = canonical_worktree(str(handoff["worktree"]))
        if pi_session_writer_may_be_alive(header, worktree, self._processes()):
            return False
        command = [
            str(self.install.node),
            str(self.helper),
            "probe",
            "--package-root",
            str(self.install.package_root),
            "--session-file",
            str(session),
            "--worktree",
            worktree,
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=15)
        if result.returncode != 0:
            return False
        try:
            payload = json.loads(result.stdout.strip())
        except json.JSONDecodeError:
            return False
        reported_cwd = payload.get("cwd") if isinstance(payload, dict) else None
        return (
            isinstance(reported_cwd, str)
            and bool(reported_cwd)
            and payload.get("session_id") == request.session_id
            and canonical_worktree(reported_cwd) == worktree
        )

    def verify(self, request: dr.ResumeRequest) -> bool:
        try:
            return self._probe(request)
        except (OSError, ServiceError, subprocess.SubprocessError):
            return False

    def resume(self, request: dr.ResumeRequest) -> dr.ResumeOutcome:
        session = self._session(request.session_id)
        worktree = canonical_worktree(str(request.handoff["worktree"]))
        event = str(request.handoff["event"])
        landing = Path(worktree) / ".nvsop" / "artifacts" / "landing"
        landing.mkdir(parents=True, exist_ok=True)
        log_path = landing / f"{event}.agent.log"
        log_fd = os.open(log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        read_fd, write_fd = os.pipe()
        os.set_inheritable(write_fd, True)
        env = dict(os.environ)
        env["NVSOP_REPAIR_ACK_FD"] = str(write_fd)
        command = [
            str(self.install.node),
            str(self.helper),
            "resume",
            "--package-root",
            str(self.install.package_root),
            "--session-file",
            str(session),
            "--worktree",
            worktree,
        ]
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                command,
                cwd=worktree,
                env=env,
                stdin=subprocess.PIPE,
                stdout=log_fd,
                stderr=log_fd,
                pass_fds=(write_fd,),
                start_new_session=True,
            )
            os.close(write_fd)
            write_fd = -1
            assert process.stdin is not None
            process.stdin.write(json.dumps(dict(request.handoff), sort_keys=True).encode("utf-8"))
            process.stdin.close()
            with selectors.DefaultSelector() as selector:
                selector.register(read_fd, selectors.EVENT_READ)
                ready = selector.select(PI_ACK_TIMEOUT_SECONDS)
            if not ready:
                if process.poll() is not None:
                    return dr.ResumeOutcome.UNAVAILABLE
                raise ServiceError("Pi resume did not acknowledge startup")
            raw = os.read(read_fd, 4096).decode("utf-8").strip()
            payload = json.loads(raw)
            reported_cwd = payload.get("cwd") if isinstance(payload, dict) else None
            if (
                not isinstance(reported_cwd, str)
                or not reported_cwd
                or payload.get("state") != "accepted"
                or payload.get("session_id") != request.session_id
                or canonical_worktree(reported_cwd) != worktree
            ):
                raise ServiceError("Pi resume acknowledgement does not match the requested session")
            self._children.append(process)
            self._children = [child for child in self._children if child.poll() is None]
            return dr.ResumeOutcome.RESUMED
        finally:
            if write_fd >= 0:
                os.close(write_fd)
            os.close(read_fd)
            os.close(log_fd)


class CommandAdapter:
    """外部 agent adapter：可执行文件以 JSON stdin 实现 `probe` / `resume` 两个动作。"""

    def __init__(self, executable: str) -> None:
        path = Path(executable).expanduser()
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            raise ServiceError("--adapter-command must name an absolute executable file")
        self.path = path
        self.name = f"command:{path.name}"

    @staticmethod
    def _payload(request: dr.ResumeRequest) -> bytes:
        return json.dumps(
            {"session_id": request.session_id, "handoff": dict(request.handoff)}, sort_keys=True
        ).encode("utf-8")

    def _call(self, action: str, request: dr.ResumeRequest) -> dict[str, object]:
        try:
            result = subprocess.run(
                [str(self.path), action],
                input=self._payload(request),
                capture_output=True,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ServiceError(f"external repair adapter {action} failed") from exc
        if result.returncode != 0:
            return {}
        try:
            value = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def verify(self, request: dr.ResumeRequest) -> bool:
        try:
            return self._call("probe", request).get("available") is True
        except ServiceError:
            return False

    def resume(self, request: dr.ResumeRequest) -> dr.ResumeOutcome:
        outcome = self._call("resume", request).get("outcome")
        return (
            dr.ResumeOutcome.RESUMED
            if outcome == dr.ResumeOutcome.RESUMED.value
            else dr.ResumeOutcome.UNAVAILABLE
        )


class AdapterBridge:
    def __init__(self, adapters: Sequence[Adapter]) -> None:
        self.adapters = tuple(adapters)
        self._selected: dict[tuple[str, str], Adapter] = {}

    @staticmethod
    def _key(request: dr.ResumeRequest) -> tuple[str, str]:
        return request.session_id, str(request.handoff.get("event", ""))

    def verify(self, request: dr.ResumeRequest) -> bool:
        matches = [adapter for adapter in self.adapters if adapter.verify(request)]
        if len(matches) > 1:
            raise ServiceError("multiple repair adapters claim the same bound session")
        if not matches:
            return False
        self._selected[self._key(request)] = matches[0]
        return True

    def resume(self, request: dr.ResumeRequest) -> dr.ResumeOutcome:
        adapter = self._selected.get(self._key(request))
        if adapter is None:
            raise ServiceError("repair adapter was not verified before resume")
        return adapter.resume(request)


def claim_path(worktree: str, event: str) -> Path:
    return Path(worktree) / ".nvsop" / "artifacts" / "landing" / f"{event}.claim"


def local_claim(worktree: str, event: str) -> str:
    path = claim_path(worktree, event)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(16)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        if path.is_symlink():
            raise ServiceError("existing local repair claim must not be a symlink") from None
        try:
            info = path.stat()
            existing = path.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise ServiceError("cannot read existing local repair claim") from exc
        if not stat.S_ISREG(info.st_mode):
            raise ServiceError("existing local repair claim must be a regular file") from None
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ServiceError("existing local repair claim permissions are too broad") from None
        if CLAIM_TOKEN.fullmatch(existing) is None:
            raise ServiceError("existing local repair claim is malformed") from None
        return existing
    try:
        os.write(descriptor, (token + "\n").encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return token


def _private_request(
    event: Mapping[str, object], target: tuple[str, str], token: str
) -> dr.ResumeRequest:
    return dr.ResumeRequest(target[1], dr.resume_handoff(event, target, token))


class RepairService:
    def __init__(
        self, backend: lq.Backend, bridge: AdapterBridge, *, only_pr: int | None = None
    ) -> None:
        self.backend = backend
        self.bridge = bridge
        self.only_pr = only_pr

    def _log(self, state: str, **fields: object) -> None:
        print(json.dumps({"state": state, **fields}, sort_keys=True), flush=True)

    def tick(self) -> int:
        state, _ = self.backend.state()
        actions = 0
        for entry in lq.ordered(state):
            if self.only_pr is not None and entry.pr != self.only_pr:
                continue
            if entry.state != lq.BLOCKED or entry.phase is not None:
                continue
            if entry.blocked_reason not in lq.REPAIR_REASONS:
                continue
            evidence = entry.evidence or {}
            repair_state = evidence.get("repair_state")
            if repair_state in lq.REPAIR_TERMINAL_STATES:
                continue
            try:
                event = lq.repair_event(self.backend, entry.pr)
                target = dr.resolve_target(event)
            except lq.InfrastructureError as exc:
                self._log("infrastructure", pr=entry.pr, error=str(exc))
                continue
            except (lq.QueueError, dr.AgentUnavailableError) as exc:
                self._log("not-local-or-stale", pr=entry.pr, error=str(exc))
                continue
            token = local_claim(target[0], str(event["event"]))
            request = _private_request(event, target, token)
            if repair_state is None:
                if not self.bridge.verify(request):
                    self._log("adapter-unavailable", pr=entry.pr)
                    continue
                dr.claim(self.backend, entry.pr, token)
                self._log("claim-requested", pr=entry.pr, event=event["event"])
                actions += 1
                continue
            digest = evidence.get("repair_claim")
            if not isinstance(digest, str) or lq.claim_digest(token) != digest:
                self._log("claim-mismatch", pr=entry.pr, event=event["event"])
                continue
            try:
                code = dr.resume(self.backend, entry.pr, self.bridge, token)
            except lq.InfrastructureError as exc:
                self._log("infrastructure", pr=entry.pr, error=str(exc))
                continue
            except (lq.QueueError, dr.DispatchError, ServiceError) as exc:
                self._log("resume-error", pr=entry.pr, error=str(exc))
                continue
            self._log("resume-step", pr=entry.pr, code=code, repair_state=repair_state)
            actions += 1
        return actions


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Run the local nvsop landing repair dispatcher service."
    )
    result.add_argument("--once", action="store_true", help="Run one queue scan and exit.")
    result.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    result.add_argument("--pr", type=int, default=None, help="Limit service actions to one PR.")
    result.add_argument(
        "--pi-command",
        default=None,
        help="Exact Pi executable; auto-detected only when unambiguous.",
    )
    result.add_argument("--pi-session-dir", default=None)
    result.add_argument(
        "--adapter-command",
        action="append",
        default=[],
        help="Absolute executable implementing JSON `probe` and `resume`; may be repeated.",
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if arguments.interval <= 0:
        print("landing_repair_service: --interval must be positive", file=sys.stderr)
        return 2
    adapters: list[Adapter] = []
    try:
        adapters.append(
            PiAdapter(command=arguments.pi_command, session_dir=arguments.pi_session_dir)
        )
    except ServiceError as exc:
        print(f"landing_repair_service: Pi adapter disabled: {exc}", file=sys.stderr)
    try:
        adapters.extend(CommandAdapter(path) for path in arguments.adapter_command)
    except ServiceError as exc:
        print(f"landing_repair_service: {exc}", file=sys.stderr)
        return 2
    if not adapters:
        print("landing_repair_service: no usable repair adapter", file=sys.stderr)
        return 2
    service = RepairService(lq.GitHub(), AdapterBridge(adapters), only_pr=arguments.pr)
    if arguments.once:
        try:
            service.tick()
            return 0
        except (lq.InfrastructureError, ServiceError) as exc:
            print(f"landing_repair_service: {exc}", file=sys.stderr)
            return 1
    while True:
        try:
            service.tick()
        except (lq.InfrastructureError, ServiceError) as exc:
            print(f"landing_repair_service: {exc}", file=sys.stderr, flush=True)
        try:
            time.sleep(arguments.interval)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
