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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dispatch_landing_repair as dr
import landing_queue as lq
from bind_task_session import canonical_worktree

CLAIM_TOKEN = re.compile(r"^[0-9a-f]{32}$")
DEFAULT_INTERVAL_SECONDS = 15.0
PI_ACK_TIMEOUT_SECONDS = 15.0


class ServiceError(RuntimeError):
    """本地 service 配置、状态或 adapter 契约错误。"""


def _read_json_line(path: Path) -> dict[str, object]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.loads(handle.readline())
    except (OSError, json.JSONDecodeError) as exc:
        raise ServiceError(f"cannot read Pi session header: {path}") from exc
    if not isinstance(value, dict):
        raise ServiceError(f"Pi session header is not an object: {path}")
    return value


def _pi_processes(helper: Path) -> list[str]:
    result: list[str] = []
    helper_name = helper.name
    for item in Path("/proc").iterdir():
        if not item.name.isdigit():
            continue
        try:
            command = (item / "comm").read_text(encoding="utf-8").strip()
            resumed_helper = False
            if command != "pi":
                arguments = [
                    os.fsdecode(value)
                    for value in (item / "cmdline").read_bytes().split(b"\0")
                    if value
                ]
                resumed_helper = (
                    len(arguments) >= 3
                    and Path(arguments[1]).name == helper_name
                    and arguments[2] == "resume"
                )
            if command == "pi" or resumed_helper:
                result.append(os.path.realpath(os.readlink(item / "cwd")))
        except OSError:
            continue
    return result


def pi_session_writer_may_be_alive(
    header: Mapping[str, object], worktree: str, processes: Sequence[str]
) -> bool:
    """Pi 无 session lock；同原 cwd/目标 worktree 的 live Pi 都视为可能写者。"""
    cwd = header.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        raise ServiceError("Pi session header has no cwd")
    protected = {canonical_worktree(cwd), canonical_worktree(worktree)}
    return any(canonical_worktree(process) in protected for process in processes)


def resolve_pi_install(command: str | None) -> tuple[Path, Path]:
    candidates: list[Path] = []
    if command:
        candidates.append(Path(command).expanduser())
    else:
        if found := shutil.which("pi"):
            candidates.append(Path(found))
        nvm = Path.home() / ".nvm" / "versions" / "node"
        if nvm.is_dir():
            candidates.extend(nvm.glob("*/bin/pi"))

    unique = {os.path.realpath(path): path for path in candidates if path.exists()}
    if len(unique) != 1:
        raise ServiceError("Pi adapter needs one unambiguous pi executable; pass --pi-command")
    launcher = next(iter(unique.values())).absolute()
    real = Path(os.path.realpath(launcher))
    if real.name != "cli.js" or real.parent.name != "bundle" or real.parent.parent.name != "dist":
        raise ServiceError("pi executable does not resolve to the supported package layout")

    node = launcher.parent / "node"
    if not node.is_file():
        found = shutil.which("node")
        if not found:
            raise ServiceError("cannot resolve the Node executable for Pi")
        node = Path(found)
    return node.resolve(strict=True), real.parents[2].resolve(strict=True)


class PiAdapter:
    def __init__(
        self,
        *,
        command: str | None = None,
        session_dir: str | None = None,
        helper: Path | None = None,
        processes: Callable[[], list[str]] | None = None,
    ) -> None:
        self.node, self.package_root = resolve_pi_install(command)
        configured = session_dir or os.environ.get("PI_CODING_AGENT_SESSION_DIR")
        agent_dir = Path(os.environ.get("PI_CODING_AGENT_DIR", Path.home() / ".pi" / "agent"))
        self.session_dir = Path(configured).expanduser() if configured else agent_dir / "sessions"
        self.helper = helper or Path(__file__).with_name("pi_resume_session.mjs")
        self._processes = processes or (lambda: _pi_processes(self.helper))
        self._sessions: dict[str, Path] = {}
        self._children: list[subprocess.Popen[bytes]] = []

    def _session(self, session_id: str) -> Path:
        if cached := self._sessions.get(session_id):
            return cached
        suffix = f"_{session_id}.jsonl"
        matches = list(self.session_dir.rglob(f"*{suffix}")) if self.session_dir.is_dir() else []
        if len(matches) != 1:
            raise ServiceError(
                f"expected exactly one Pi session file for {session_id}, found {len(matches)}"
            )
        session = matches[0].resolve(strict=True)
        header = _read_json_line(session)
        if header.get("type") != "session" or header.get("id") != session_id:
            raise ServiceError("Pi session header does not match the bound session id")
        self._sessions[session_id] = session
        return session

    def _command(self, action: str, session: Path, worktree: str) -> list[str]:
        return [
            str(self.node),
            str(self.helper),
            action,
            "--package-root",
            str(self.package_root),
            "--session-file",
            str(session),
            "--worktree",
            worktree,
        ]

    @staticmethod
    def _accepted(payload: object, request: dr.ResumeRequest, worktree: str) -> bool:
        if not isinstance(payload, dict):
            return False
        cwd = payload.get("cwd")
        return (
            isinstance(cwd, str)
            and bool(cwd)
            and payload.get("session_id") == request.session_id
            and canonical_worktree(cwd) == worktree
        )

    def verify(self, request: dr.ResumeRequest) -> bool:
        try:
            session = self._session(request.session_id)
            header = _read_json_line(session)
            worktree = canonical_worktree(str(request.handoff["worktree"]))
            if pi_session_writer_may_be_alive(header, worktree, self._processes()):
                return False
            result = subprocess.run(
                self._command("probe", session, worktree),
                capture_output=True,
                text=True,
                check=False,
                timeout=PI_ACK_TIMEOUT_SECONDS,
            )
            return result.returncode == 0 and self._accepted(
                json.loads(result.stdout.strip()), request, worktree
            )
        except (OSError, ServiceError, subprocess.SubprocessError, json.JSONDecodeError):
            return False

    def resume(self, request: dr.ResumeRequest) -> dr.ResumeOutcome:
        session = self._session(request.session_id)
        worktree = canonical_worktree(str(request.handoff["worktree"]))
        read_fd, write_fd = os.pipe()
        os.set_inheritable(write_fd, True)
        env = {**os.environ, "NVSOP_REPAIR_ACK_FD": str(write_fd)}
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                self._command("resume", session, worktree),
                cwd=worktree,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                pass_fds=(write_fd,),
                start_new_session=True,
            )
            os.close(write_fd)
            write_fd = -1
            assert process.stdin is not None
            process.stdin.write(json.dumps(dict(request.handoff), sort_keys=True).encode())
            process.stdin.close()
            with selectors.DefaultSelector() as selector:
                selector.register(read_fd, selectors.EVENT_READ)
                ready = selector.select(PI_ACK_TIMEOUT_SECONDS)
            if not ready:
                if process.poll() is not None:
                    return dr.ResumeOutcome.UNAVAILABLE
                raise ServiceError("Pi resume did not acknowledge startup")
            payload = json.loads(os.read(read_fd, 4096).decode().strip())
            if payload.get("state") != "accepted" or not self._accepted(payload, request, worktree):
                raise ServiceError("Pi resume acknowledgement does not match the requested session")
            self._children = [child for child in self._children if child.poll() is None]
            self._children.append(process)
            return dr.ResumeOutcome.RESUMED
        finally:
            if write_fd >= 0:
                os.close(write_fd)
            os.close(read_fd)


class CommandAdapter:
    """外部 agent adapter：可执行文件以 JSON stdin 实现 probe / resume。"""

    def __init__(self, executable: str) -> None:
        path = Path(executable).expanduser()
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            raise ServiceError("--adapter-command must name an absolute executable file")
        self.path = path

    @staticmethod
    def _payload(request: dr.ResumeRequest) -> bytes:
        return json.dumps(
            {"session_id": request.session_id, "handoff": dict(request.handoff)}, sort_keys=True
        ).encode()

    def _call(self, action: str, request: dr.ResumeRequest) -> dict[str, object]:
        try:
            result = subprocess.run(
                [str(self.path), action],
                input=self._payload(request),
                capture_output=True,
                check=False,
                timeout=PI_ACK_TIMEOUT_SECONDS,
            )
            value = json.loads(result.stdout.decode()) if result.returncode == 0 else {}
        except (OSError, subprocess.SubprocessError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def verify(self, request: dr.ResumeRequest) -> bool:
        return self._call("probe", request).get("available") is True

    def resume(self, request: dr.ResumeRequest) -> dr.ResumeOutcome:
        return (
            dr.ResumeOutcome.RESUMED
            if self._call("resume", request).get("outcome") == dr.ResumeOutcome.RESUMED.value
            else dr.ResumeOutcome.UNAVAILABLE
        )


class AdapterBridge:
    def __init__(self, adapters: Sequence[dr.HostBridge]) -> None:
        self.adapters = tuple(adapters)
        self._selected: dict[tuple[str, str], dr.HostBridge] = {}

    def verify(self, request: dr.ResumeRequest) -> bool:
        matches = [adapter for adapter in self.adapters if adapter.verify(request)]
        if len(matches) > 1:
            raise ServiceError("multiple repair adapters claim the same bound session")
        if not matches:
            return False
        key = request.session_id, str(request.handoff.get("event", ""))
        self._selected[key] = matches[0]
        return True

    def resume(self, request: dr.ResumeRequest) -> dr.ResumeOutcome:
        key = request.session_id, str(request.handoff.get("event", ""))
        adapter = self._selected.get(key)
        if adapter is None:
            raise ServiceError("repair adapter was not verified before resume")
        return adapter.resume(request)


def claim_path(worktree: str, event: str) -> Path:
    return Path(worktree) / ".nvsop" / "artifacts" / "landing" / f"{event}.claim"


def claim_request_path(worktree: str, event: str) -> Path:
    return Path(worktree) / ".nvsop" / "artifacts" / "landing" / f"{event}.claim-requested"


def claim_request_recorded(worktree: str, event: str) -> bool:
    return os.path.lexists(claim_request_path(worktree, event))


def record_claim_request(worktree: str, event: str) -> None:
    path = claim_request_path(worktree, event)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return
    except OSError as exc:
        raise ServiceError("cannot persist local repair claim request receipt") from exc
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o077
            or CLAIM_TOKEN.fullmatch(existing) is None
        ):
            raise ServiceError("existing local repair claim is not a private valid token") from None
        return existing
    try:
        os.write(descriptor, (token + "\n").encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return token


class RepairService:
    def __init__(
        self, backend: lq.Backend, bridge: AdapterBridge, *, only_pr: int | None = None
    ) -> None:
        self.backend = backend
        self.bridge = bridge
        self.only_pr = only_pr

    @staticmethod
    def _log(state: str, **fields: object) -> None:
        print(json.dumps({"state": state, **fields}, sort_keys=True), flush=True)

    def tick(self) -> int:
        state, _ = self.backend.state()
        actions = 0
        for entry in lq.ordered(state):
            if self.only_pr is not None and entry.pr != self.only_pr:
                continue
            if (
                entry.state != lq.BLOCKED
                or entry.phase is not None
                or entry.blocked_reason not in lq.REPAIR_REASONS
            ):
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

            event_id = str(event["event"])
            token = local_claim(target[0], event_id)
            request = dr.ResumeRequest(target[1], dr.resume_handoff(event, target, token))
            if repair_state is None:
                if claim_request_recorded(target[0], event_id):
                    self._log("claim-pending", pr=entry.pr, event=event_id)
                    continue
                if not self.bridge.verify(request):
                    self._log("adapter-unavailable", pr=entry.pr)
                    continue
                try:
                    dr.claim(self.backend, entry.pr, token)
                except lq.InfrastructureError as exc:
                    self._log("infrastructure", pr=entry.pr, error=str(exc))
                    continue
                except (lq.QueueError, dr.DispatchError) as exc:
                    self._log("claim-error", pr=entry.pr, error=str(exc))
                    continue
                record_claim_request(target[0], event_id)
                self._log("claim-requested", pr=entry.pr, event=event_id)
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
    result = argparse.ArgumentParser(description="Run the local landing repair dispatcher.")
    result.add_argument("--once", action="store_true")
    result.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    result.add_argument("--pr", type=int)
    result.add_argument("--pi-command")
    result.add_argument("--pi-session-dir")
    result.add_argument("--adapter-command", action="append", default=[])
    return result


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if arguments.interval <= 0:
        print("landing_repair_service: --interval must be positive", file=sys.stderr)
        return 2
    adapters: list[dr.HostBridge] = []
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
        except (lq.QueueError, dr.DispatchError, ServiceError) as exc:
            print(f"landing_repair_service: {exc}", file=sys.stderr)
            return 1
    while True:
        try:
            service.tick()
        except (lq.QueueError, dr.DispatchError, ServiceError) as exc:
            print(f"landing_repair_service: {exc}", file=sys.stderr, flush=True)
        try:
            time.sleep(arguments.interval)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
