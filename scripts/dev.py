#!/usr/bin/env python3
"""Issue #119 的固定 main 开发实例编排器。

本地入口默认使用 HTTP；显式设置 `NVSOP_DEV_PROTOCOL=https` 才启用固定 main 本地 TLS。
本脚本只编排 Git 快照、Tilt、Docker Compose 和测试命令；产品业务仍由各应用提供。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import IO
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import UUID

try:
    import fcntl
except ImportError:  # pragma: no cover - 目标开发环境是 WSL2
    fcntl = None  # type: ignore[assignment]

_MODULE_DIRECTORY = Path(__file__).resolve().parent
_MODULE_PARENT = _MODULE_DIRECTORY.parent
if str(_MODULE_PARENT) not in sys.path:
    sys.path.insert(0, str(_MODULE_PARENT))

# isort: off
from scripts.dev_manual_test import (  # noqa: E402
    execute_manual_test as _execute_manual_test,
    failed_test_entry as _failed_test_entry,
    interrupted_test_entry as _interrupted_test_entry,
    record_test_result as _record_test_result,
    test_state as _test_state,
    test_stop_event as _test_stop_event,
)
from scripts.dev_process import process_alive as _process_alive  # noqa: E402
from scripts.dev_process import process_matches as _process_matches  # noqa: E402
from scripts.dev_process import signal_process as _signal_process  # noqa: E402
from scripts.dev_snapshot import (  # noqa: E402
    MissingLfsError,
    SnapshotError as _SnapshotError,
    SnapshotInterrupted as _SnapshotInterrupted,
    archive_environment as _archive_environment,
    archive_main as _archive_main,
    hydrate_lfs_files as _hydrate_lfs_files,
    lfs_files as _lfs_files,
    lfs_media_directory as _lfs_media_directory,
    read_snapshot_manifest,
    snapshot_manifest,
)

# isort: on

PROJECT_NAME = "nvsop-dev-main"
TILT_VERSION = "0.37.7"
TILT_PORT = 10350
BUSINESS_PORT = 8443
MEDIA_PORT = 8444
PLAYWRIGHT_UI_PORT = 9323
PLAYWRIGHT_UI_URL = f"http://localhost:{PLAYWRIGHT_UI_PORT}"
PROTOCOL_ENVIRONMENT = "NVSOP_DEV_PROTOCOL"
TLS_CERT_ENVIRONMENT = "NVSOP_DEV_TLS_CERT_FILE"
TLS_KEY_ENVIRONMENT = "NVSOP_DEV_TLS_KEY_FILE"
TLS_CA_ENVIRONMENT = "NVSOP_DEV_TLS_CA_FILE"
GATEWAY_TLS_CA_PATH = "/etc/nginx/tls/ca.crt"
GATEWAY_SYSTEM_CA_NAME = "system-ca.crt"
GATEWAY_SYSTEM_CA_PATH = "/etc/nginx/tls/system-ca.crt"
HASHED_CERTIFICATE_ENTRY = re.compile(r"^[0-9a-fA-F]{8}\.\d+$")
TLS_METADATA_FILE = "tls.json"
DEFAULT_PROTOCOL = "http"
SUPPORTED_PROTOCOLS = frozenset({"http", "https"})
# 已登记来源推理机 UUID → 浏览器可达证据服务 origin 的部署配置；中心与网关 CSP 同源于此。
EVIDENCE_ORIGINS_ENVIRONMENT = "SOP_EVIDENCE_HOST_ORIGINS"
# origin 只允许 scheme、主机（含 IPv6 字面量）和端口；拒绝空白/引号/分号/$/通配符等注入字符。
EVIDENCE_ORIGIN_PATTERN = re.compile(r"https?://[A-Za-z0-9.\-\[\]:]+")
CSP_ADD_HEADER_PATTERN = re.compile(r'add_header Content-Security-Policy "([^"]*)" always;')
CSP_EVIDENCE_DIRECTIVES = ("media-src", "connect-src")
POLL_SECONDS = 2
READY_TIMEOUT_SECONDS = 900


class DevError(RuntimeError):
    """开发环境操作无法安全继续。"""


class DevInterrupted(KeyboardInterrupt):
    """收到停止请求，调用方应进入项目级清理。"""


def configured_protocol(environ: Mapping[str, str] | None = None) -> str:
    """读取本地入口协议；HTTP 是默认值，HTTPS 只在显式选择时启用。"""
    values = os.environ if environ is None else environ
    protocol = values.get(PROTOCOL_ENVIRONMENT, DEFAULT_PROTOCOL).strip().lower()
    if protocol not in SUPPORTED_PROTOCOLS:
        choices = ", ".join(sorted(SUPPORTED_PROTOCOLS))
        raise DevError(f"{PROTOCOL_ENVIRONMENT} 必须是 {choices} 之一，实际为：{protocol!r}")
    return protocol


def public_urls(protocol: str) -> dict[str, str]:
    """生成固定开发实例的浏览器入口地址。"""
    if protocol not in SUPPORTED_PROTOCOLS:
        raise DevError(f"不支持的开发协议：{protocol}")
    return {
        "business": f"{protocol}://localhost:{BUSINESS_PORT}",
        "annotation_media": f"{protocol}://localhost:{MEDIA_PORT}",
        "tilt": f"http://localhost:{TILT_PORT}",
        "playwright_ui": PLAYWRIGHT_UI_URL,
    }


def state_protocol(item: DevPaths) -> str:
    """读取运行实例已经使用的协议；缺失字段仅兼容早期 HTTPS 状态文件。"""
    raw = read_state(item).get("protocol")
    if raw is None:
        return "https"
    if not isinstance(raw, str) or raw not in SUPPORTED_PROTOCOLS:
        raise DevError(f"开发实例状态中的协议无效：{raw!r}")
    return raw


@dataclass(frozen=True)
class DevPaths:
    """固定开发实例的所有状态路径。"""

    root: Path
    state: Path

    @property
    def snapshots(self) -> Path:
        return self.state / "snapshots"

    @property
    def logs(self) -> Path:
        return self.state / "logs"

    @property
    def reports(self) -> Path:
        return self.state / "reports"

    @property
    def secrets(self) -> Path:
        return self.state / "secrets"

    @property
    def tls(self) -> Path:
        return self.state / "tls"

    @property
    def samples(self) -> Path:
        return self.state / "samples"

    @property
    def state_file(self) -> Path:
        return self.state / "state.json"

    @property
    def setup_file(self) -> Path:
        return self.state / "setup.json"

    @property
    def launcher_pid(self) -> Path:
        return self.state / "launcher.pid"

    @property
    def tilt_pid(self) -> Path:
        return self.state / "tilt.pid"

    @property
    def refresh_file(self) -> Path:
        return self.state / "refresh.requested"

    @property
    def operation_lock(self) -> Path:
        return self.state / "operation.lock"

    @property
    def credentials_file(self) -> Path:
        return self.state / "credentials.txt"

    @property
    def compose_file(self) -> Path:
        return self.root / "deploy" / "dev" / "compose.yaml"

    @property
    def tiltfile(self) -> Path:
        return self.root / "Tiltfile"


class ExclusiveLock:
    """用一个文件锁把更新、冒烟和浏览器测试串行化。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._file: IO[str] | None = None

    def acquire(self, *, blocking: bool) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+")
        if fcntl is None:  # pragma: no cover - WSL2 使用 fcntl
            self._file = handle
            return True
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError:
            handle.close()
            return False
        self._file = handle
        return True

    def release(self) -> None:
        if self._file is None:
            return
        handle = self._file
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        self._file = None

    def __enter__(self) -> ExclusiveLock:
        if not self.acquire(blocking=True):  # pragma: no cover - blocking lock
            raise DevError(f"无法取得开发实例锁：{self._path}")
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class LauncherPid:
    """持有 OS 文件锁并写入诊断 PID，避免两个固定实例同时运行。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._file: IO[str] | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._file is not None:
            raise DevError("固定开发实例 launcher 已由当前进程持有")
        if fcntl is None:  # pragma: no cover - WSL2 使用 fcntl
            try:
                descriptor = os.open(
                    self._path,
                    os.O_CREAT | os.O_EXCL | os.O_RDWR,
                    0o600,
                )
            except FileExistsError as error:
                pid = read_pid(self._path)
                detail = f"PID {pid}" if pid is not None else "未知 PID"
                raise DevError(f"固定开发实例已经运行或锁文件残留（{detail}）") from error
            self._file = os.fdopen(descriptor, "w+")
        else:
            handle = self._path.open("a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                handle.seek(0)
                raw_pid = handle.read().strip()
                handle.close()
                detail = f"PID {raw_pid}" if raw_pid else "未知 PID"
                raise DevError(f"固定开发实例已经运行（{detail}）") from error
            self._file = handle
        self._file.seek(0)
        self._file.truncate()
        self._file.write(str(os.getpid()))
        self._file.flush()

    def release(self) -> None:
        handle = self._file
        if handle is None:
            return
        try:
            handle.seek(0)
            owns_file = handle.read().strip() == str(os.getpid())
            if owns_file:
                self._path.unlink(missing_ok=True)
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            self._file = None


def launcher_is_locked(path: Path) -> bool:
    """判断 PID 文件是否仍由一个活跃 launcher 持有，而不是相信陈旧 PID。"""
    if fcntl is None:  # pragma: no cover - 目标开发环境是 WSL2
        return path.exists()
    if not path.exists():
        return False
    handle = path.open("a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def paths(root: Path | None = None, state: Path | None = None) -> DevPaths:
    repository = (root or Path(__file__).resolve().parents[1]).resolve()
    configured = state or (
        Path(os.environ["NVSOP_DEV_STATE_DIR"])
        if os.environ.get("NVSOP_DEV_STATE_DIR")
        else repository / ".nvsop" / "dev-main"
    )
    return DevPaths(root=repository, state=configured.expanduser().resolve())


def initial_state(protocol: str | None = None) -> dict[str, object]:
    selected = protocol or configured_protocol()
    urls = public_urls(selected)
    return {
        "schema": 1,
        "project": PROJECT_NAME,
        "protocol": selected,
        "status": "stopped",
        "target_sha": None,
        "running_sha": None,
        "updated_at": None,
        "failure": None,
        "snapshot": None,
        "test": None,
        "last_smoke": None,
        "last_ui": None,
        "links": urls,
    }


def read_state(item: DevPaths) -> dict[str, object]:
    if not item.state_file.is_file():
        return initial_state()
    try:
        value = json.loads(item.state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DevError(f"开发实例状态文件不可读：{item.state_file}") from error
    if not isinstance(value, dict):
        raise DevError(f"开发实例状态文件不是对象：{item.state_file}")
    return value


def write_state(item: DevPaths, value: dict[str, object]) -> None:
    item.state.mkdir(parents=True, exist_ok=True)
    temporary = item.state_file.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(item.state_file)


def change_state(item: DevPaths, **changes: object) -> dict[str, object]:
    value = read_state(item)
    value.update(changes)
    value["updated_at"] = utc_now()
    write_state(item, value)
    return value


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def ensure_directories(item: DevPaths) -> None:
    for directory in (
        item.state,
        item.snapshots,
        item.logs,
        item.reports,
        item.secrets,
        item.tls,
        item.samples,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if os.name != "nt":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    else:  # pragma: no cover - 目标开发环境是 WSL2
        with contextlib.suppress(ProcessLookupError):
            process.terminate()


def run_checked(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    stdout: int | IO[bytes] | None = subprocess.PIPE,
    stderr: int | IO[bytes] | None = subprocess.PIPE,
    stop_event: Event | None = None,
) -> subprocess.CompletedProcess[bytes]:
    if stop_event is None:
        try:
            return subprocess.run(
                arguments,
                cwd=cwd,
                env=env,
                check=False,
                timeout=timeout,
                stdout=stdout,
                stderr=stderr,
            )
        except subprocess.TimeoutExpired as error:
            raise DevError(f"命令超时：{' '.join(arguments)}") from error
        except OSError as error:
            raise DevError(f"无法执行命令：{' '.join(arguments)}：{error}") from error

    if stop_event.is_set():
        raise DevInterrupted("收到停止请求")
    deadline = time.monotonic() + timeout if timeout is not None else None
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            arguments,
            cwd=cwd,
            env=env,
            stdout=stdout,
            stderr=stderr,
            start_new_session=os.name != "nt",
        )
        while True:
            try:
                output, error_output = process.communicate(timeout=0.2)
                if stop_event.is_set():
                    raise DevInterrupted("收到停止请求")
                return subprocess.CompletedProcess(
                    arguments, process.returncode, output, error_output
                )
            except subprocess.TimeoutExpired:
                if stop_event.is_set():
                    _terminate_process(process)
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                    raise DevInterrupted("收到停止请求") from None
                if deadline is not None and time.monotonic() >= deadline:
                    _terminate_process(process)
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                    raise DevError(f"命令超时：{' '.join(arguments)}") from None
    except KeyboardInterrupt:
        if process is not None and process.poll() is None:
            _terminate_process(process)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.communicate(timeout=5)
        raise
    except OSError as error:
        raise DevError(f"无法执行命令：{' '.join(arguments)}：{error}") from error


def require_ports(ports: Mapping[str, int]) -> None:
    """在启动进程前检查固定端口，并报告占用端口对应的服务。"""
    occupied = [f"{name}={port}" for name, port in ports.items() if not port_available(port)]
    if occupied:
        raise DevError("固定开发实例端口已被占用：" + ", ".join(occupied))


def port_available(port: int) -> bool:
    """检查本机 IPv4 回环地址上的 TCP 端口能否绑定。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def require_instance_ports() -> None:
    require_ports(
        {
            "Tilt": TILT_PORT,
            "业务网关": BUSINESS_PORT,
            "媒体网关": MEDIA_PORT,
        }
    )


def require_browser_port() -> None:
    require_ports({"Playwright UI": PLAYWRIGHT_UI_PORT})


def require_tools(*, protocol: str | None = None, include_browser: bool = True) -> None:
    selected = protocol or configured_protocol()
    required = ["git", "git-lfs", "docker", "ffmpeg", "ffprobe", "pnpm", "uv", "tilt"]
    if selected == "https":
        required.insert(2, "openssl")
    if include_browser:
        required.append("node")
    missing = [command for command in required if not command_exists(command)]
    if missing:
        raise DevError("缺少必需工具：" + ", ".join(missing))
    compose = run_checked(["docker", "compose", "version"])
    if compose.returncode != 0:
        detail = compose.stderr.decode("utf-8", errors="replace").strip()
        raise DevError(f"Docker Compose 不可用：{detail}")
    daemon = run_checked(["docker", "info"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if daemon.returncode != 0:
        detail = daemon.stderr.decode("utf-8", errors="replace").strip()
        raise DevError(f"Docker daemon 不可用：{detail}")
    tilt = run_checked(["tilt", "version"])
    version_text = (tilt.stdout + tilt.stderr).decode("utf-8", errors="replace")
    if tilt.returncode != 0 or TILT_VERSION not in version_text:
        raise DevError(f"需要 Tilt v{TILT_VERSION}，实际响应为：{version_text.strip()}")
    require_instance_ports()
    if include_browser:
        require_browser_port()


def require_main_checkout(item: DevPaths) -> None:
    """固定实例只允许从主工作树的 main 分支启动。"""
    branch = run_checked(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], cwd=item.root)
    if branch.returncode != 0:
        raise DevError("固定开发实例只能从本地 main 分支启动；当前 checkout 不是分支")
    current_branch = branch.stdout.decode("utf-8", errors="replace").strip()
    if current_branch != "main":
        raise DevError(
            f"固定开发实例只能从本地 main 分支启动；当前分支为 {current_branch or '<unknown>'}"
        )
    top_level = run_checked(["git", "rev-parse", "--show-toplevel"], cwd=item.root)
    if top_level.returncode != 0:
        detail = top_level.stderr.decode("utf-8", errors="replace").strip()
        raise DevError(f"无法确认 main 工作树：{detail}")
    actual_root = Path(top_level.stdout.decode("utf-8", errors="replace").strip()).resolve()
    if actual_root != item.root:
        raise DevError(f"固定开发实例必须从主工作树启动：{item.root}")


def main_sha(item: DevPaths) -> str:
    require_main_checkout(item)
    result = run_checked(
        ["git", "rev-parse", "--verify", "refs/heads/main^{commit}"], cwd=item.root
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise DevError(f"本地 main 不存在或不是提交：{detail}")
    return result.stdout.decode("ascii").strip()


def archive_environment(environ: Mapping[str, str]) -> dict[str, str]:
    return _archive_environment(environ)


def lfs_media_directory(item: DevPaths, *, stop_event: Event | None = None) -> Path | None:
    try:
        return _lfs_media_directory(item.root, run_checked=run_checked, stop_event=stop_event)
    except _SnapshotError as error:
        raise DevError(str(error)) from error


def lfs_files(item: DevPaths, sha: str, *, stop_event: Event | None = None) -> list[dict[str, str]]:
    try:
        return _lfs_files(item.root, sha, run_checked=run_checked, stop_event=stop_event)
    except _SnapshotError as error:
        raise DevError(str(error)) from error


def hydrate_lfs_files(
    snapshot: Path,
    entries: list[dict[str, str]],
    *,
    media_directory: Path | None,
) -> None:
    try:
        _hydrate_lfs_files(snapshot, entries, media_directory=media_directory)
    except _SnapshotError as error:
        raise DevError(str(error)) from error


def archive_main(
    item: DevPaths,
    sha: str,
    *,
    stop_event: Event | None = None,
) -> Path:
    ensure_directories(item)
    try:
        return _archive_main(
            item,
            sha,
            run_checked=run_checked,
            stop_event=stop_event,
        )
    except MissingLfsError:
        raise
    except _SnapshotInterrupted as error:
        raise DevInterrupted(str(error)) from error
    except _SnapshotError as error:
        raise DevError(str(error)) from error


def runtime_environment(
    item: DevPaths, *, sha: str, source: Path, protocol: str | None = None
) -> dict[str, str]:
    selected = protocol or configured_protocol()
    urls = public_urls(selected)
    environment = os.environ.copy()
    environment.update(
        {
            "COMPOSE_PROJECT_NAME": PROJECT_NAME,
            "NVSOP_SOURCE_DIR": str(source),
            "NVSOP_DEV_STATE_DIR": str(item.state),
            "NVSOP_DEV_NGINX_CONFIG": str(item.state / "nginx.conf"),
            "NVSOP_TARGET_SHA": sha,
            "NVSOP_DEV_LOGIN_NAME": dev_login_name(item),
            "NVSOP_LAUNCHER_SCRIPT": str(source / "scripts" / "dev.py"),
            "NVSOP_HOST_LAUNCHER_SCRIPT": str(item.root / "scripts" / "dev.py"),
            "NVSOP_DEV_PROTOCOL": selected,
            "NVSOP_DEV_COOKIE_TRANSPORT": (
                "require_https" if selected == "https" else "allow_http"
            ),
            "NVSOP_DEV_BASE_URL": urls["business"],
            "NVSOP_DEV_MEDIA_URL": urls["annotation_media"],
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    ca = configured_ca_certificate(item) if selected == "https" else None
    if ca is not None:
        environment["SSL_CERT_FILE"] = str(ca)
        healthcheck_args = f"--cacert {GATEWAY_TLS_CA_PATH}"
    elif selected == "https":
        # HTTPS 无自定义 CA 时保留继承的 SSL_CERT_FILE/SSL_CERT_DIR，并让网关
        # 使用同一份暂存的系统信任根，避免容器内 curl 缺少宿主私有 CA。
        healthcheck_args = (
            f"--cacert {GATEWAY_SYSTEM_CA_PATH}"
            if (item.tls / GATEWAY_SYSTEM_CA_NAME).is_file()
            else ""
        )
    else:
        environment.pop("SSL_CERT_FILE", None)
        healthcheck_args = ""
    environment["NVSOP_DEV_TLS_HEALTHCHECK_ARGS"] = healthcheck_args
    return environment


def dev_login_name(item: DevPaths) -> str:
    return os.environ.get("NVSOP_DEV_LOGIN_NAME", "dev.admin")


def compose_command(item: DevPaths, *arguments: str, source: Path | None = None) -> list[str]:
    compose_file = (source or item.root) / "deploy" / "dev" / "compose.yaml"
    return [
        "docker",
        "compose",
        "--project-name",
        PROJECT_NAME,
        "--file",
        str(compose_file),
        *arguments,
    ]


def compose_environment(item: DevPaths, *, sha: str | None = None) -> dict[str, str]:
    value = read_state(item)
    target = sha or str(value.get("target_sha") or value.get("running_sha") or "local")
    source = item.snapshots / target
    if not source.is_dir():
        source = item.root
    environment = runtime_environment(
        item,
        sha=target,
        source=source,
        protocol=state_protocol(item),
    )
    return environment


def write_secret(path: Path, value: str) -> None:
    if path.exists():
        return
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)


def tls_input_path(environ: Mapping[str, str], name: str) -> Path | None:
    """读取显式外部 TLS 输入路径；空值表示未配置。"""
    raw = environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else None


def public_key(path: Path, *, certificate: bool) -> bytes | None:
    command = (
        ["openssl", "x509", "-in", str(path), "-pubkey", "-noout"]
        if certificate
        else ["openssl", "pkey", "-in", str(path), "-pubout", "-passin", "pass:"]
    )
    result = run_checked(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        return None
    return result.stdout


def private_key_is_valid(path: Path) -> bool:
    """校验私钥可用；-passin pass: 让加密私钥立即失败而不是交互式索要口令。"""
    result = run_checked(
        ["openssl", "pkey", "-in", str(path), "-noout", "-check", "-passin", "pass:"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.returncode == 0


def server_certificate_error(cert: Path, key: Path, ca: Path | None) -> str | None:
    """校验显式外部服务端材料；返回拒绝原因，通过时返回 None。

    cert 可以是含中间证书的完整链；ca 为显式自定义信任锚，缺省时使用系统信任库。
    """
    if not cert.is_file() or cert.stat().st_size == 0:
        return f"证书文件不存在或为空：{cert}"
    if not key.is_file() or key.stat().st_size == 0:
        return f"私钥文件不存在或为空：{key}"
    if ca is not None and (not ca.is_file() or ca.stat().st_size == 0):
        return f"CA 文件不存在或为空：{ca}"
    if not private_key_is_valid(key):
        return f"私钥无效、不可读或已加密：{key}"
    certificate_public_key = public_key(cert, certificate=True)
    if certificate_public_key is None:
        return f"证书文件不可解析或不是 PEM 证书：{cert}"
    private_public_key = public_key(key, certificate=False)
    if private_public_key is None or certificate_public_key != private_public_key:
        return f"证书与私钥不匹配：{cert} / {key}"
    command = [
        "openssl",
        "verify",
        "-untrusted",
        str(cert),
        "-purpose",
        "sslserver",
        "-verify_hostname",
        "localhost",
    ]
    if ca is not None:
        # 显式自定义 CA 时必须禁用默认 CApath/CAstore，否则 -CAfile 会回退到系统信任库。
        command += ["-no-CApath", "-no-CAstore", "-CAfile", str(ca)]
    command.append(str(cert))
    result = run_checked(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        lines = [
            line
            for line in result.stderr.decode("utf-8", errors="replace").splitlines()
            if line.strip()
        ]
        detail = next(
            (line for line in lines if line.startswith("error ") and " at " in line),
            lines[-1] if lines else "验证失败",
        )
        return f"证书链或主机名 localhost 校验失败：{detail}"
    return None


def atomic_write(path: Path, data: bytes, *, mode: int) -> None:
    """原子写入状态目录文件，不写穿已存在的符号链接。"""
    handle, temporary_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def stage_tls_file(source: Path, target: Path, *, mode: int) -> None:
    """把已校验的外部材料原子复制到状态目录；不修改输入，也不写穿目标符号链接。"""
    atomic_write(target, source.read_bytes(), mode=mode)


def _hashed_certificate_files(directory: Path) -> list[Path]:
    """列出 OpenSSL 哈希目录中的证书索引条目，忽略其它文件与 CRL。"""
    if not directory.is_dir():
        return []
    return [
        child
        for child in sorted(directory.iterdir())
        if child.is_file() and HASHED_CERTIFICATE_ENTRY.match(child.name)
    ]


def effective_system_trust_pem() -> bytes | None:
    """把宿主当前生效的系统信任根原样拼接成单个 PEM；无可用根时返回 None。

    只复制既有信任根，不生成、不签名、不导入任何 CA。OpenSSL 的 capath 惰性加载，
    因此按哈希目录语义只读取索引条目（`<8位十六进制>.<整数>`），不扫描目录内其它 PEM。
    """
    paths = ssl.get_default_verify_paths()
    sources: list[Path] = []
    cafile = os.environ.get(paths.openssl_cafile_env, paths.openssl_cafile or "")
    if cafile:
        sources.append(Path(cafile))
    capath = os.environ.get(paths.openssl_capath_env, paths.openssl_capath or "")
    for entry in capath.split(os.pathsep):
        if entry:
            sources.extend(_hashed_certificate_files(Path(entry)))
    parts: list[bytes] = []
    for source in sources:
        try:
            parts.append(source.read_bytes())
        except OSError:
            continue
    if not parts:
        return None
    return b"\n".join(parts)


def stage_system_trust(item: DevPaths) -> Path:
    """把生效的系统信任根暂存到 tls/ 供容器网关使用；无法读取任何根时明确拒绝。"""
    bundle = effective_system_trust_pem()
    if bundle is None:
        raise DevError(
            "HTTPS 未提供 NVSOP_DEV_TLS_CA_FILE，且无法从系统信任库读取任何根证书；"
            "请显式提供 NVSOP_DEV_TLS_CA_FILE"
        )
    target = item.tls / GATEWAY_SYSTEM_CA_NAME
    atomic_write(target, bundle, mode=0o644)
    return target


def write_tls_metadata(item: DevPaths, ca: Path | None) -> None:
    item.state.mkdir(parents=True, exist_ok=True)
    (item.state / TLS_METADATA_FILE).write_text(
        json.dumps({"ca_certificate": str(ca) if ca is not None else None}) + "\n",
        encoding="utf-8",
    )


def configured_ca_certificate(item: DevPaths) -> Path | None:
    """读取显式选择的自定义 CA；未配置返回 None，元数据损坏或文件缺失则拒绝。"""
    path = item.state / TLS_METADATA_FILE
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DevError(f"TLS 元数据不可读：{path}") from error
    if not isinstance(value, dict):
        raise DevError(f"TLS 元数据不是对象：{path}")
    raw = value.get("ca_certificate")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise DevError(f"TLS 元数据中的 CA 路径无效：{raw!r}")
    ca = Path(raw)
    if not ca.is_file():
        raise DevError(f"已配置的自定义 CA 不存在：{ca}；请重新运行 make dev-setup")
    return ca


def prepare_tls(item: DevPaths, environ: Mapping[str, str] | None = None) -> Path | None:
    """校验并暂存显式外部 TLS 材料；缺失 cert/key 时拒绝，绝不自动生成或修复。

    输入文件只读复制，不修改；返回暂存的自定义 CA 路径，未配置时为 None。
    """
    values = os.environ if environ is None else environ
    cert = tls_input_path(values, TLS_CERT_ENVIRONMENT)
    key = tls_input_path(values, TLS_KEY_ENVIRONMENT)
    ca = tls_input_path(values, TLS_CA_ENVIRONMENT)
    if cert is None or key is None:
        raise DevError(
            "HTTPS 需要显式提供外部证书和私钥："
            f"{TLS_CERT_ENVIRONMENT} 与 {TLS_KEY_ENVIRONMENT}；不会自动生成或修复开发证书"
        )
    error = server_certificate_error(cert, key, ca)
    if error is not None:
        raise DevError(f"外部 TLS 材料不可用：{error}")
    stage_tls_file(cert, item.tls / "dev.crt", mode=0o644)
    stage_tls_file(key, item.tls / "dev.key", mode=0o600)
    staged_ca: Path | None = None
    if ca is not None:
        staged_ca = item.tls / "ca.crt"
        stage_tls_file(ca, staged_ca, mode=0o644)
    else:
        stage_system_trust(item)
    write_tls_metadata(item, staged_ca)
    return staged_ca


def evidence_source_origins(environ: Mapping[str, str], *, protocol: str) -> list[str]:
    """读取并严格校验部署来源映射；非法输入直接拒绝，不做弱默认降级。

    值与中心 `Settings.evidence_host_origins` 同源同义：规范 UUID → 单一 HTTP(S)
    origin，不含路径、凭据、查询或片段。网关必须独立拒绝可能注入 CSP/Nginx 的字符。
    未设置该变量时保持既有行为；显式空值/空白是配置错误，不是“未配置”。
    错误消息不回显 origin/键的原始值，避免把误填凭据写进普通错误输出。
    """
    if EVIDENCE_ORIGINS_ENVIRONMENT not in environ:
        return []
    raw = environ[EVIDENCE_ORIGINS_ENVIRONMENT]
    if not raw.strip():
        raise DevError(
            f"{EVIDENCE_ORIGINS_ENVIRONMENT} 已显式设置为空；请提供合法 JSON 对象或移除该变量"
        )
    try:
        mapping = json.loads(raw)
    except json.JSONDecodeError as error:
        raise DevError(f"{EVIDENCE_ORIGINS_ENVIRONMENT} 不是合法 JSON 对象") from error
    if not isinstance(mapping, dict):
        raise DevError(f"{EVIDENCE_ORIGINS_ENVIRONMENT} 必须是 UUID→origin 的 JSON 对象")
    origins: list[str] = []
    for index, (host_id, origin) in enumerate(mapping.items(), start=1):
        if not isinstance(host_id, str) or not isinstance(origin, str):
            raise DevError(
                f"{EVIDENCE_ORIGINS_ENVIRONMENT} 的键必须是 UUID、值必须是 origin 字符串"
            )
        try:
            canonical = str(UUID(host_id)) == host_id
        except ValueError:
            canonical = False
        if not canonical:
            raise DevError(f"第 {index} 个证据来源主机必须是规范 UUID")
        try:
            parsed = urlsplit(origin)
        except ValueError as error:
            raise DevError(f"第 {index} 个证据来源 origin 无法解析") from error
        try:
            port = parsed.port
        except ValueError as error:
            raise DevError(f"第 {index} 个证据来源 origin 端口非法") from error
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or (port is not None and not 1 <= port <= 65535)
            or origin != f"{parsed.scheme}://{parsed.netloc}"
            or not EVIDENCE_ORIGIN_PATTERN.fullmatch(origin)
        ):
            raise DevError(
                f"第 {index} 个证据来源 origin 必须是单一 HTTP(S) origin，且不含路径/凭据/查询/片段"
            )
        if parsed.scheme == "http" and protocol != "http":
            raise DevError("HTTP 证据来源 origin 只允许用于 HTTP 本地入口")
        if origin not in origins:
            origins.append(origin)
    return origins


def inject_csp_evidence_origins(text: str, origins: list[str]) -> str:
    """把来源 origin 追加到网关 CSP 的 connect-src/media-src，保留其余指令与顺序。"""
    if not origins:
        return text
    token = " ".join(origins)

    def replace(match: re.Match[str]) -> str:
        csp = match.group(1)
        for directive in CSP_EVIDENCE_DIRECTIVES:
            pattern = re.compile(rf"({directive} [^;]*)")
            csp, count = pattern.subn(lambda item: f"{item.group(1)} {token}", csp, count=1)
            if count != 1:
                raise DevError(f"内容安全策略缺少唯一的 {directive} 指令，无法追加证据来源")
        return f'add_header Content-Security-Policy "{csp}" always;'

    replaced, count = CSP_ADD_HEADER_PATTERN.subn(replace, text)
    if count != 1:
        raise DevError("已配置证据来源 origin，但网关模板缺少唯一的 Content-Security-Policy 指令")
    return replaced


def render_gateway_config(
    item: DevPaths,
    *,
    source: Path,
    protocol: str,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """按协议渲染 Nginx 入口；HTTP 不读取也不要求证书，HTTPS 只接受显式外部材料。"""
    if protocol not in SUPPORTED_PROTOCOLS:
        raise DevError(f"不支持的开发协议：{protocol}")
    template = source / "deploy" / "dev" / "nginx.conf"
    if not template.is_file():
        raise DevError(f"开发网关配置不存在：{template}")
    text = template.read_text(encoding="utf-8")
    if "__NVSOP_LISTEN_SUFFIX__" not in text or "# __NVSOP_TLS_DIRECTIVES__" not in text:
        raise DevError(f"开发网关配置缺少协议占位符：{template}")
    origins = evidence_source_origins(os.environ if environ is None else environ, protocol=protocol)
    text = inject_csp_evidence_origins(text, origins)
    ca: Path | None = None
    if protocol == "https":
        ca = prepare_tls(item)
        listen_suffix = " ssl"
        tls_directives = (
            "    ssl_certificate /etc/nginx/tls/dev.crt;\n"
            "    ssl_certificate_key /etc/nginx/tls/dev.key;\n"
        )
    else:
        listen_suffix = ""
        tls_directives = ""
    rendered = text.replace("__NVSOP_LISTEN_SUFFIX__", listen_suffix).replace(
        "    # __NVSOP_TLS_DIRECTIVES__\n", tls_directives
    )
    output = item.state / "nginx.conf"
    temporary = output.with_suffix(".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(output)
    return ca


def ensure_sample_video(item: DevPaths) -> None:
    target = item.samples / "dev-sample.mp4"
    if target.exists() and target.stat().st_size > 0:
        return
    result = run_checked(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=10",
            "-t",
            "3",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(target),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0 or not target.is_file():
        raise DevError(f"无法生成合成视频：{result.stderr.decode(errors='replace')}")


def ensure_credentials(item: DevPaths) -> None:
    write_secret(item.secrets / "center-db-password", "center-" + os.urandom(18).hex())
    write_secret(item.secrets / "nvsop-runtime-password", "nvsop-runtime-" + os.urandom(18).hex())
    write_secret(
        item.secrets / "training-runtime-password",
        "training-runtime-" + os.urandom(18).hex(),
    )
    write_secret(item.secrets / "bootstrap-password", "dev-" + os.urandom(24).hex())
    write_secret(item.secrets / "csrf-secret", os.urandom(32).hex())
    write_secret(item.secrets / "redis-url", "redis://redis:6379/0")
    if not item.credentials_file.exists():
        item.credentials_file.write_text(
            f"login_name={dev_login_name(item)}\n"
            f"password_file={item.secrets / 'bootstrap-password'}\n",
            encoding="utf-8",
        )
        item.credentials_file.chmod(0o600)


def setup(item: DevPaths) -> None:
    protocol = configured_protocol()
    ensure_directories(item)
    require_tools(protocol=protocol)
    ensure_credentials(item)
    staged_ca = render_gateway_config(item, source=item.root, protocol=protocol)
    ensure_sample_video(item)
    install = run_checked(
        ["pnpm", "install", "--frozen-lockfile", "--prefer-offline"], cwd=item.root
    )
    if install.returncode != 0:
        raise DevError(
            "pnpm 依赖准备失败：" + install.stderr.decode("utf-8", errors="replace").strip()
        )
    sync = run_checked(["uv", "sync", "--frozen", "--all-packages"], cwd=item.root)
    if sync.returncode != 0:
        raise DevError("uv 依赖准备失败：" + sync.stderr.decode("utf-8", errors="replace").strip())
    environment = runtime_environment(item, sha="local", source=item.root, protocol=protocol)
    pulled = run_checked(
        compose_command(
            item,
            "pull",
            "center-db",
            "redis",
            "gateway",
        ),
        cwd=item.root,
        env=environment,
    )
    if pulled.returncode != 0:
        raise DevError("固定开发实例基础镜像准备失败；请查看 Docker 输出后重试")
    item.setup_file.write_text(
        json.dumps(
            {
                "schema": 1,
                "project": PROJECT_NAME,
                "created_at": utc_now(),
                "protocol": protocol,
                "ca_certificate": str(staged_ca) if staged_ca is not None else None,
                "gateway_config": str(item.state / "nginx.conf"),
                "credentials_file": str(item.credentials_file),
                "sample_video": str(item.samples / "dev-sample.mp4"),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    write_state(item, initial_state(protocol))
    print(f"开发实例准备完成：{item.state}")
    print(f"协议：{protocol}；入口：{public_urls(protocol)['business']}")
    print(f"开发账号信息文件：{item.credentials_file}")
    if protocol == "https" and staged_ca is not None:
        certificate = item.tls / "dev.crt"
        print(f"已使用显式自定义 CA：{staged_ca}；浏览器需信任该 CA，服务端证书：{certificate}")


def require_setup(item: DevPaths, *, runtime_credentials: bool = True) -> None:
    protocol = configured_protocol()
    if not item.setup_file.is_file():
        raise DevError(f"请先运行 make dev-setup；未找到 {item.setup_file}")
    required = [item.secrets / "bootstrap-password"]
    if runtime_credentials:
        # S065 的 runtime 密码只在启动/运行路径需要；`logs`/`down` 不该因既有实例缺少它们而失败。
        required += [
            item.secrets / "nvsop-runtime-password",
            item.secrets / "training-runtime-password",
        ]
    required += [item.state / "nginx.conf", item.samples / "dev-sample.mp4"]
    if protocol == "https":
        required.extend((item.tls / "dev.crt", item.tls / "dev.key"))
        configured_ca_certificate(item)
    for path in required:
        if not path.is_file():
            mode = (
                "https 模式请使用 NVSOP_DEV_PROTOCOL=https 重新运行 make dev-setup"
                if protocol == "https"
                else "请重新运行 make dev-setup"
            )
            raise DevError(f"开发实例准备不完整，缺少：{path}；{mode}")


def service_rows(
    item: DevPaths, *, stop_event: Event | None = None
) -> tuple[list[dict[str, object]], str | None]:
    environment = compose_environment(item)
    source = Path(environment["NVSOP_SOURCE_DIR"])
    result = run_checked(
        compose_command(item, "ps", "--all", "--format", "json", source=source),
        cwd=source,
        env=environment,
        stop_event=stop_event,
    )
    if result.returncode != 0:
        return [], result.stderr.decode("utf-8", errors="replace").strip()
    text = result.stdout.decode("utf-8", errors="replace").strip()
    if not text:
        return [], None
    try:
        document: object = json.loads(text)
        if isinstance(document, list):
            raw_rows = document
        else:
            raw_rows = [json.loads(line) for line in text.splitlines()]
        rows = [row for row in raw_rows if isinstance(row, dict)]
    except json.JSONDecodeError:
        rows = []
        for line in text.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows, None


def service_status(rows: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for row in rows:
        name = row.get("Service") or row.get("Name")
        if isinstance(name, str):
            result[name] = {
                "state": row.get("State"),
                "health": row.get("Health"),
                "exit_code": row.get("ExitCode"),
                "container": row.get("Name"),
            }
    return result


_REQUIRED_READY_SERVICES = frozenset(
    {
        "center-db",
        "redis",
        "annotation-backend",
        "annotation-frontend",
        "center-api",
        "worker",
        "web",
        "gateway",
    }
)


def status_exit_code(
    value: Mapping[str, object], services: Mapping[str, Mapping[str, object]]
) -> int:
    """把状态 JSON 中的失败、未就绪和 liveness 失败传给命令调用方。"""
    if value.get("status") in {"failed", "degraded", "starting", "updating"}:
        return 1
    if value.get("status") != "ready":
        return 0
    liveness = value.get("api_liveness")
    if not isinstance(liveness, Mapping) or liveness.get("ok") is not True:
        return 1
    if any(
        services.get(name, {}).get("state") != "running"
        or services.get(name, {}).get("health") != "healthy"
        for name in _REQUIRED_READY_SERVICES
    ):
        return 1
    return 0


def http_probe(item: DevPaths, url: str) -> dict[str, object]:
    try:
        request = Request(url, headers={"Accept": "application/json"})
        if urlsplit(url).scheme == "https":
            ca = configured_ca_certificate(item)
            context = ssl.create_default_context(cafile=str(ca) if ca is not None else None)
            response_context = urlopen(request, context=context, timeout=5)
        else:
            response_context = urlopen(request, timeout=5)
        with response_context as response:
            body = response.read(4096).decode("utf-8", errors="replace")
            return {"ok": 200 <= response.status < 300, "status": response.status, "body": body}
    except (OSError, URLError, ssl.SSLError) as error:
        return {"ok": False, "error": str(error)}


def status(item: DevPaths) -> int:
    value = read_state(item)
    rows, compose_error = service_rows(item) if item.setup_file.exists() else ([], None)
    services = service_status(rows)
    launcher = pid_alive(item.launcher_pid)
    tilt = pid_alive(item.tilt_pid)
    value["launcher"] = {"pid": read_pid(item.launcher_pid), "alive": launcher}
    value["tilt"] = {"pid": read_pid(item.tilt_pid), "alive": tilt}
    protocol = state_protocol(item)
    urls = public_urls(protocol)
    value["protocol"] = protocol
    value["links"] = urls
    value["services"] = services
    value["api_liveness"] = http_probe(item, f"{urls['business']}/api/v1/liveness")
    value["dependency_readiness"] = {
        name: {
            "state": details.get("state"),
            "health": details.get("health"),
        }
        for name, details in services.items()
        if name in {"center-db", "redis", "center-api", "worker"}
    }
    if compose_error:
        value["compose_error"] = compose_error
    # 先按既有判据 + compose_error 计算一次退出码；只读地改写内存展示状态，绝不回写 state 文件。
    result = 1 if compose_error is not None else status_exit_code(value, services)
    if result != 0 and value.get("status") == "ready":
        value["status"] = "degraded"
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    return result


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="ascii"))
    except (OSError, ValueError):
        return None


def pid_alive(path: Path) -> bool:
    pid = read_pid(path)
    return pid is not None and process_alive(pid)


def process_alive(pid: int) -> bool:
    return _process_alive(pid)


def process_matches(item: DevPaths, pid: int, *, command_name: str) -> bool:
    return _process_matches(item.root, pid, command_name=command_name)


def launcher_process_matches(item: DevPaths, pid: int) -> bool:
    return process_matches(item, pid, command_name="run")


def test_process_matches(item: DevPaths, pid: int, *, kind: str) -> bool:
    return process_matches(item, pid, command_name="smoke" if kind == "smoke" else "test-ui")


def signal_process(pid: int, signum: signal.Signals) -> bool:
    return _signal_process(pid, signum)


def signal_launcher(item: DevPaths, pid: int, signum: signal.Signals) -> bool:
    """只向仍持有 launcher 文件锁且命令行匹配的进程发信号。"""
    if not launcher_is_locked(item.launcher_pid):
        return False
    if not launcher_process_matches(item, pid):
        raise DevError(f"launcher PID {pid} 与固定开发实例不匹配，未发送信号")
    return signal_process(pid, signum)


def start_tilt(item: DevPaths, *, sha: str, source: Path, protocol: str) -> subprocess.Popen[bytes]:
    try:
        log = (item.logs / "tilt.log").open("ab")
    except OSError as error:
        raise DevError(f"无法打开 Tilt 日志：{error}") from error
    environment = runtime_environment(item, sha=sha, source=source, protocol=protocol)
    try:
        process = subprocess.Popen(
            [
                "tilt",
                "up",
                "--file",
                str(source / "Tiltfile"),
                "--host",
                "127.0.0.1",
                "--port",
                str(TILT_PORT),
                "--stream",
            ],
            cwd=source,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    except OSError as error:
        log.close()
        raise DevError(f"无法启动 Tilt：{error}") from error
    log.close()
    try:
        item.tilt_pid.write_text(str(process.pid), encoding="ascii")
    except OSError as error:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        raise DevError(f"无法写入 Tilt PID：{error}") from error
    return process


def stop_tilt(item: DevPaths, process: subprocess.Popen[bytes] | None = None) -> None:
    pid = process.pid if process is not None else read_pid(item.tilt_pid)
    if pid is None:
        item.tilt_pid.unlink(missing_ok=True)
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGINT)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.25)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
    item.tilt_pid.unlink(missing_ok=True)


def compose_down(item: DevPaths) -> None:
    environment = compose_environment(item)
    source = Path(environment["NVSOP_SOURCE_DIR"])
    result = run_checked(
        compose_command(item, "down", "--remove-orphans", source=source),
        cwd=source,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise DevError(f"停止固定开发实例失败（数据卷未删除）：{detail}")


def failed_service(statuses: dict[str, dict[str, object]], name: str) -> bool:
    details = statuses.get(name)
    return (
        details is not None
        and details.get("state") == "exited"
        and details.get("exit_code")
        not in {
            0,
            "0",
            None,
        }
    )


def tilt_resource_failure(name: str, *, stop_event: Event | None = None) -> str | None:
    result = run_checked(
        [
            "tilt",
            "get",
            "uiresource",
            name,
            "--host",
            "127.0.0.1",
            "--port",
            str(TILT_PORT),
            "--output",
            "json",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5,
        stop_event=stop_event,
    )
    if result.returncode != 0:
        return None
    try:
        value = json.loads(result.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    status = value.get("status")
    if not isinstance(status, dict):
        return None
    if status.get("updateStatus") == "error":
        history = status.get("buildHistory")
        if isinstance(history, list) and history and isinstance(history[-1], dict):
            detail = history[-1].get("error")
            if isinstance(detail, str) and detail:
                return detail
        return f"Tilt resource {name} 更新失败"
    if status.get("runtimeStatus") == "error":
        return f"Tilt resource {name} 运行失败"
    return None


def wait_until_ready(
    item: DevPaths,
    *,
    sha: str,
    protocol: str,
    stop_event: Event | None = None,
) -> tuple[bool, str | None]:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    required_running = _REQUIRED_READY_SERVICES
    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            raise DevInterrupted("收到停止请求")
        rows, error = service_rows(item, stop_event=stop_event)
        if error is not None:
            if stop_event is not None and stop_event.wait(POLL_SECONDS):
                raise DevInterrupted("收到停止请求")
            time.sleep(0 if stop_event is not None else POLL_SECONDS)
            continue
        statuses = service_status(rows)
        if read_pid(item.tilt_pid) is not None and not pid_alive(item.tilt_pid):
            return False, "tilt"
        sample_failure = tilt_resource_failure("sample-data", stop_event=stop_event)
        if sample_failure is not None:
            return False, "samples"
        if failed_service(statuses, "center-migrate"):
            return False, "migration"
        if failed_service(statuses, "center-bootstrap"):
            return False, "initialization"
        for name in required_running:
            if failed_service(statuses, name):
                return False, name
        healthy = all(
            statuses.get(name, {}).get("state") == "running"
            and statuses.get(name, {}).get("health") == "healthy"
            for name in required_running
        )
        urls = public_urls(protocol)
        liveness = http_probe(item, f"{urls['business']}/api/v1/liveness").get("ok") is True
        if healthy and liveness:
            sample = item.reports / f"sample-{sha}.json"
            if sample.is_file():
                try:
                    sample_value = json.loads(sample.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    sample_value = {}
                if isinstance(sample_value, dict):
                    if sample_value.get("status") == "passed":
                        return True, None
                    if sample_value.get("status") in {"failed", "aborted"}:
                        return False, "samples"
        if stop_event is not None:
            if stop_event.wait(POLL_SECONDS):
                raise DevInterrupted("收到停止请求")
        else:
            time.sleep(POLL_SECONDS)
    return False, "readiness"


def clean_snapshot_overlays(item: DevPaths) -> None:
    """移除可视化测试在提交快照中安装的依赖，避免污染后续 Docker build context。"""
    for snapshot in item.snapshots.iterdir():
        if not snapshot.is_dir() or snapshot.name.startswith("."):
            continue
        for path in (
            snapshot / "node_modules",
            snapshot / "apps" / "control-web" / "node_modules",
        ):
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)


def build_target(
    item: DevPaths,
    *,
    sha: str,
    source: Path,
    protocol: str,
    stop_event: Event | None = None,
) -> Path | None:
    if stop_event is not None and stop_event.is_set():
        raise DevInterrupted("收到停止请求")
    clean_snapshot_overlays(item)
    environment = runtime_environment(item, sha=sha, source=source, protocol=protocol)
    log_path = item.logs / f"build-{sha}.log"
    with log_path.open("wb") as log:
        arguments = ["build", "--parallel"]
        profiles = set(environment.get("COMPOSE_PROFILES", "").split(","))
        if profiles & {"training-gpu", "*"}:
            prepared = run_checked(
                [sys.executable, str(source / "scripts/training_dependencies.py"), "--prepare"],
                cwd=source,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                stop_event=stop_event,
            )
            if prepared.returncode != 0:
                return None
            # CUDA 扩展和 GPU 镜像构建串行；默认 core 构建保持原行为。
            environment["COMPOSE_PARALLEL_LIMIT"] = "1"
            arguments = ["build"]
        result = run_checked(
            compose_command(item, *arguments, source=source),
            cwd=source,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            stop_event=stop_event,
        )
    if result.returncode == 0:
        return log_path
    return None


def migrate_target(
    item: DevPaths,
    *,
    sha: str,
    source: Path,
    protocol: str,
    stop_event: Event | None = None,
) -> Path | None:
    """在新目标应用启动前完成迁移；失败不得放行目标服务。"""
    environment = runtime_environment(item, sha=sha, source=source, protocol=protocol)
    log_path = item.logs / f"migrate-{sha}.log"
    with log_path.open("wb") as log:
        result = run_checked(
            compose_command(
                item,
                "up",
                "--no-build",
                "--exit-code-from",
                "center-migrate",
                "center-migrate",
                source=source,
            ),
            cwd=source,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            stop_event=stop_event,
        )
    return log_path if result.returncode == 0 else None


def stale_test_entry(entry: object, *, current_sha: str) -> object:
    """把旧提交的测试结果显式标成需重测，而不是继续显示为当前通过。"""
    if not isinstance(entry, dict) or entry.get("tested_sha") == current_sha:
        return entry
    if entry.get("status") == "stale":
        return entry
    stale = dict(entry)
    stale.update(
        {
            "status": "stale",
            "needs_rerun": True,
            "current_sha": current_sha,
            "stale_at": utc_now(),
        }
    )
    return stale


def update_to(
    item: DevPaths,
    *,
    sha: str,
    protocol: str,
    force: bool = False,
    stop_event: Event | None = None,
) -> bool:
    value = read_state(item)
    if (
        not force
        and value.get("running_sha") == sha
        and value.get("status") == "ready"
        and value.get("protocol") == protocol
    ):
        return True

    stale_smoke = stale_test_entry(value.get("last_smoke"), current_sha=sha)
    stale_ui = stale_test_entry(value.get("last_ui"), current_sha=sha)
    change_state(
        item,
        status="updating",
        protocol=protocol,
        links=public_urls(protocol),
        target_sha=sha,
        failure=None,
        test=None,
        snapshot=None,
        last_smoke=stale_smoke,
        last_ui=stale_ui,
    )
    try:
        snapshot = archive_main(
            item,
            sha,
            stop_event=stop_event,
        )
        snapshot_info = read_snapshot_manifest(snapshot)
        change_state(item, snapshot=snapshot_info)
        if stop_event is not None and stop_event.is_set():
            raise DevInterrupted("收到停止请求")
        render_gateway_config(item, source=snapshot, protocol=protocol)
        build_log = build_target(
            item,
            sha=sha,
            source=snapshot,
            protocol=protocol,
            stop_event=stop_event,
        )
    except DevInterrupted:
        raise
    except MissingLfsError as error:
        failure_manifest = snapshot_manifest(
            sha=sha,
            missing_lfs_paths=error.missing_lfs_paths,
            warning=str(error),
        )
        change_state(
            item,
            status="failed",
            target_sha=sha,
            running_sha=value.get("running_sha"),
            snapshot=failure_manifest,
            failure={
                "phase": "snapshot",
                "target_sha": sha,
                "detail": str(error),
                "missing_lfs_paths": error.missing_lfs_paths,
                "at": utc_now(),
            },
        )
        return False
    except DevError as error:
        change_state(
            item,
            status="failed",
            target_sha=sha,
            running_sha=value.get("running_sha"),
            failure={"phase": "prepare", "target_sha": sha, "detail": str(error), "at": utc_now()},
        )
        return False

    if build_log is None:
        change_state(
            item,
            status="failed",
            target_sha=sha,
            running_sha=value.get("running_sha"),
            failure={
                "phase": "build",
                "target_sha": sha,
                "exit_code": 1,
                "log": str(item.logs / f"build-{sha}.log"),
                "at": utc_now(),
            },
        )
        return False
    if stop_event is not None and stop_event.is_set():
        raise DevInterrupted("收到停止请求")
    stop_tilt(item)
    try:
        compose_down(item)
    except DevError as error:
        change_state(
            item,
            status="failed",
            target_sha=sha,
            running_sha=value.get("running_sha"),
            failure={"phase": "stop", "target_sha": sha, "detail": str(error), "at": utc_now()},
        )
        return False
    change_state(
        item,
        status="starting",
        protocol=protocol,
        target_sha=sha,
        failure=None,
        last_smoke=stale_smoke,
        last_ui=stale_ui,
    )
    migration_log = migrate_target(
        item, sha=sha, source=snapshot, protocol=protocol, stop_event=stop_event
    )
    if migration_log is None:
        change_state(
            item,
            status="failed",
            target_sha=sha,
            running_sha=None,
            failure={
                "phase": "migration",
                "target_sha": sha,
                "exit_code": 1,
                "log": str(item.logs / f"migrate-{sha}.log"),
                "at": utc_now(),
            },
        )
        return False
    try:
        start_tilt(item, sha=sha, source=snapshot, protocol=protocol)
        ready, phase = wait_until_ready(item, sha=sha, protocol=protocol, stop_event=stop_event)
    except DevInterrupted:
        raise
    except DevError as error:
        with contextlib.suppress(DevError):
            stop_tilt(item)
        with contextlib.suppress(DevError):
            compose_down(item)
        change_state(
            item,
            status="failed",
            target_sha=sha,
            running_sha=None,
            failure={
                "phase": "readiness",
                "target_sha": sha,
                "detail": str(error),
                "build_log": str(build_log),
                "at": utc_now(),
            },
        )
        return False
    if not ready:
        with contextlib.suppress(DevError):
            stop_tilt(item)
        with contextlib.suppress(DevError):
            compose_down(item)
        change_state(
            item,
            status="failed",
            target_sha=sha,
            running_sha=None,
            failure={
                "phase": phase or "readiness",
                "target_sha": sha,
                "build_log": str(build_log),
                "at": utc_now(),
            },
        )
        return False
    change_state(item, status="ready", target_sha=sha, running_sha=sha, failure=None)
    return True


def run_loop(item: DevPaths) -> None:
    protocol = configured_protocol()
    require_setup(item)
    require_tools(protocol=protocol, include_browser=False)
    ensure_directories(item)
    owner = LauncherPid(item.launcher_pid)
    owner.acquire()
    stopping = Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stopping.set()

    def request_refresh(_signum: int, _frame: object) -> None:
        item.refresh_file.touch()

    old_int = signal.signal(signal.SIGINT, request_stop)
    old_term = signal.signal(signal.SIGTERM, request_stop)
    old_usr = signal.signal(getattr(signal, "SIGUSR1", signal.SIGTERM), request_refresh)
    try:
        lock = acquire_operation_lock(item, stop_event=stopping)
        try:
            target = main_sha(item)
            if not update_to(
                item,
                sha=target,
                protocol=protocol,
                force=True,
                stop_event=stopping,
            ):
                print(
                    "固定开发实例更新失败，旧实例状态已保留；请运行 make dev-refresh",
                    file=sys.stderr,
                )
        finally:
            lock.release()
        while not stopping.wait(POLL_SECONDS):
            target = main_sha(item)
            value = read_state(item)
            requested = item.refresh_file.exists()
            failed_same_target = (
                value.get("status") == "failed"
                and value.get("target_sha") == target
                and value.get("running_sha") != target
            )
            needs_update = (
                requested or value.get("target_sha") != target or value.get("running_sha") != target
            )
            if needs_update and (requested or not failed_same_target):
                lock = ExclusiveLock(item.operation_lock)
                if lock.acquire(blocking=False):
                    try:
                        # 重新读取 SHA 和状态；等待测试锁期间 main 可能已经前进。
                        target = main_sha(item)
                        value = read_state(item)
                        requested = item.refresh_file.exists()
                        failed_same_target = (
                            value.get("status") == "failed"
                            and value.get("target_sha") == target
                            and value.get("running_sha") != target
                        )
                        needs_update = (
                            requested
                            or value.get("target_sha") != target
                            or value.get("running_sha") != target
                        )
                        if needs_update and (requested or not failed_same_target):
                            item.refresh_file.unlink(missing_ok=True)
                            update_to(
                                item,
                                sha=target,
                                protocol=protocol,
                                force=requested,
                                stop_event=stopping,
                            )
                    finally:
                        lock.release()
                continue
            tilt_pid = read_pid(item.tilt_pid)
            if tilt_pid is not None and not pid_alive(item.tilt_pid):
                change_state(
                    item,
                    status="degraded",
                    failure={
                        "phase": "tilt",
                        "detail": "Tilt 已退出；Compose 资源未被误判为已停止",
                        "at": utc_now(),
                    },
                )
    finally:
        # 测试进程可能持有 operation_lock；先取消它，再进入项目级收尾。
        with contextlib.suppress(DevError):
            cancel_test(item)
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(getattr(signal, "SIGUSR1", signal.SIGTERM), old_usr)
        try:
            with ExclusiveLock(item.operation_lock):
                stop_tilt(item)
                compose_down(item)
                change_state(item, status="stopped", running_sha=None, test=None)
        finally:
            owner.release()


def refresh(item: DevPaths) -> None:
    pid = read_pid(item.launcher_pid)
    if pid is None or not launcher_is_locked(item.launcher_pid):
        raise DevError("固定开发实例 launcher 未运行；先运行 make dev")
    item.refresh_file.touch()
    signum = getattr(signal, "SIGUSR1", signal.SIGTERM)
    if not signal_launcher(item, pid, signum):
        raise DevError("固定开发实例 launcher 已退出；请重新运行 make dev")
    print(json.dumps({"requested": True, "target_sha": main_sha(item)}, ensure_ascii=False))


def report_path(item: DevPaths, prefix: str, sha: str) -> Path:
    return item.reports / f"{prefix}-{sha}.json"


def playwright_report_spec_keys(
    payload: object,
) -> frozenset[tuple[str, int, int, str]] | None:
    if not isinstance(payload, Mapping):
        return None
    raw_suites = payload.get("suites")
    if not isinstance(raw_suites, list):
        return None

    keys: set[tuple[str, int, int, str]] = set()
    stack: list[object] = list(raw_suites)
    while stack:
        suite = stack.pop()
        if not isinstance(suite, Mapping):
            return None
        nested = suite.get("suites", [])
        specs = suite.get("specs", [])
        if not isinstance(nested, list) or not isinstance(specs, list):
            return None
        stack.extend(nested)
        for spec in specs:
            if not isinstance(spec, Mapping):
                return None
            file = spec.get("file")
            line = spec.get("line")
            column = spec.get("column")
            title = spec.get("title")
            if (
                not isinstance(file, str)
                or not file
                or not isinstance(line, int)
                or isinstance(line, bool)
                or line < 1
                or not isinstance(column, int)
                or isinstance(column, bool)
                or column < 1
                or not isinstance(title, str)
                or not title
            ):
                return None
            keys.add((file, line, column, title))
    return frozenset(keys)


def playwright_ui_expected_specs(
    web_root: Path, environment: Mapping[str, str]
) -> frozenset[tuple[str, int, int, str]]:
    result = run_checked(
        [
            "pnpm",
            "exec",
            "playwright",
            "test",
            "--list",
            "--reporter=json",
            "--config",
            str(web_root / "playwright.config.ts"),
        ],
        cwd=web_root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise DevError(f"枚举可视化测试失败：{detail}")
    try:
        payload = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DevError("枚举可视化测试未返回有效 JSON") from error
    specs = playwright_report_spec_keys(payload)
    if not specs:
        raise DevError("枚举可视化测试未发现任何测试")
    return specs


def playwright_ui_report_passed(
    report: Path, expected_specs: frozenset[tuple[str, int, int, str]]
) -> bool:
    try:
        payload = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, Mapping):
        return False
    stats = payload.get("stats")
    if not isinstance(stats, Mapping):
        return False
    counts = (stats.get("expected"), stats.get("skipped"), stats.get("unexpected"))
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in counts
    ):
        return False
    expected, _skipped, unexpected = counts
    actual_specs = playwright_report_spec_keys(payload)
    return unexpected == 0 and expected > 0 and actual_specs == expected_specs


def ready_instance(item: DevPaths, *, action: str) -> tuple[str, Path, str, dict[str, str]]:
    """在互斥锁内读取被测 SHA，避免测试跨越一次实例更新。"""
    value = read_state(item)
    sha = value.get("running_sha")
    if not isinstance(sha, str) or value.get("status") != "ready":
        raise DevError(f"固定开发实例尚未 ready，不能{action}")
    snapshot = item.snapshots / sha
    if not snapshot.is_dir():
        raise DevError(f"固定开发实例缺少运行提交快照：{snapshot}")
    protocol = state_protocol(item)
    return sha, snapshot, protocol, public_urls(protocol)


def test_state(item: DevPaths, *, kind: str, sha: str, protocol: str, report: Path) -> None:
    _test_state(
        item,
        kind=kind,
        sha=sha,
        protocol=protocol,
        report=report,
        change_state=change_state,
        now=utc_now,
    )


def cancel_test(item: DevPaths, *, timeout: float = 15.0) -> None:
    """停止测试子进程并等待其释放 operation_lock，避免收尾永久等待。"""
    value = read_state(item)
    raw_test = value.get("test")
    if not isinstance(raw_test, Mapping):
        return
    kind = raw_test.get("kind")
    pid = raw_test.get("pid")
    if not isinstance(kind, str) or not isinstance(pid, int) or pid == os.getpid():
        change_state(item, test=None)
        return
    if not test_process_matches(item, pid, kind=kind):
        change_state(item, test=None)
        return
    signal_process(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = read_state(item).get("test")
        if not isinstance(current, Mapping) or not process_alive(pid):
            return
        time.sleep(0.1)
    signal_process(pid, signal.SIGKILL)


def record_test_result(item: DevPaths, *, kind: str, entry: dict[str, object]) -> None:
    _record_test_result(item, kind=kind, entry=entry, change_state=change_state)


@contextlib.contextmanager
def test_stop_event() -> Iterator[Event]:
    with _test_stop_event() as stopping:
        yield stopping


def interrupted_test_entry(
    *,
    sha: str,
    protocol: str,
    command: list[str],
    report: Path,
    log: Path | None = None,
) -> dict[str, object]:
    return _interrupted_test_entry(
        sha=sha,
        protocol=protocol,
        command=command,
        report=report,
        now=utc_now,
        log=log,
    )


def failed_test_entry(
    *,
    sha: str,
    protocol: str,
    command: list[str],
    report: Path,
    error: Exception,
    log: Path | None = None,
) -> dict[str, object]:
    return _failed_test_entry(
        sha=sha,
        protocol=protocol,
        command=command,
        report=report,
        error=error,
        now=utc_now,
        log=log,
    )


def execute_manual_test(
    item: DevPaths,
    *,
    kind: str,
    sha: str,
    protocol: str,
    command: list[str],
    report: Path,
    execute: Callable[[Event], subprocess.CompletedProcess[bytes]],
    log: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return _execute_manual_test(
        item,
        kind=kind,
        sha=sha,
        protocol=protocol,
        command=command,
        report=report,
        execute=execute,
        change_state=change_state,
        now=utc_now,
        log=log,
    )


def run_smoke(item: DevPaths) -> None:
    require_setup(item)
    with ExclusiveLock(item.operation_lock):
        sha, snapshot, protocol, urls = ready_instance(item, action="运行功能冒烟")
        script = snapshot / "scripts" / "dev_smoke.py"
        report = report_path(item, "smoke", sha)
        command = [
            sys.executable,
            str(script),
            "--base-url",
            urls["business"],
            "--login-name",
            dev_login_name(item),
            "--password-file",
            str(item.secrets / "bootstrap-password"),
        ]
        staged_ca = configured_ca_certificate(item) if protocol == "https" else None
        if staged_ca is not None:
            command += ["--ca-file", str(staged_ca)]
        command += ["--report-file", str(report)]
        output = item.logs / f"smoke-{sha}.log"

        def execute(stopping: Event) -> subprocess.CompletedProcess[bytes]:
            environment = runtime_environment(item, sha=sha, source=snapshot, protocol=protocol)
            with output.open("wb") as log:
                return run_checked(
                    command,
                    cwd=snapshot,
                    env=environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stop_event=stopping,
                )

        result = execute_manual_test(
            item,
            kind="smoke",
            sha=sha,
            protocol=protocol,
            command=command,
            report=report,
            execute=execute,
            log=output,
        )
        entry: dict[str, object] = {
            "tested_sha": sha,
            "protocol": protocol,
            "status": "passed" if result.returncode == 0 else "failed",
            "exit_code": result.returncode,
            "command": command,
            "log": str(output),
            "report": str(report),
            "finished_at": utc_now(),
        }
        record_test_result(item, kind="smoke", entry=entry)
        if result.returncode != 0:
            raise DevError(f"功能冒烟失败，报告：{report}")
        print(json.dumps(entry, ensure_ascii=False, indent=2))


def ensure_snapshot_node_modules(
    item: DevPaths, *, sha: str, snapshot: Path, protocol: str
) -> None:
    """只在选定提交快照内解析 Web 依赖，不把当前工作树挂入测试实例。"""
    destination = snapshot / "apps" / "control-web" / "node_modules"
    if destination.is_dir() and not destination.is_symlink():
        return
    if destination.is_symlink():
        destination.unlink()
    environment = runtime_environment(item, sha=sha, source=snapshot, protocol=protocol)
    environment.pop("NODE_ENV", None)
    result = run_checked(
        ["pnpm", "install", "--frozen-lockfile", "--prefer-offline"],
        cwd=snapshot,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise DevError(f"提交快照 Web 依赖准备失败：{detail}")
    if not destination.is_dir() or destination.is_symlink():
        raise DevError("提交快照 Web 依赖准备后仍缺少 node_modules")


def run_ui(item: DevPaths) -> None:
    require_setup(item)
    with ExclusiveLock(item.operation_lock):
        require_browser_port()
        sha, snapshot, protocol, urls = ready_instance(item, action="打开可视化测试")
        ensure_snapshot_node_modules(item, sha=sha, snapshot=snapshot, protocol=protocol)
        report = report_path(item, "ui", sha)
        report.unlink(missing_ok=True)
        output_dir = item.reports / "ui" / sha
        output_dir.mkdir(parents=True, exist_ok=True)
        web_root = snapshot / "apps" / "control-web"
        environment = runtime_environment(item, sha=sha, source=snapshot, protocol=protocol)
        environment.pop("NODE_ENV", None)
        environment.pop("PLAYWRIGHT_JSON_OUTPUT_FILE", None)
        environment.update(
            {
                "NVSOP_DEV": "1",
                "NVSOP_BASE_URL": urls["business"],
            }
        )
        discovery_environment = environment.copy()
        environment.update(
            {
                "PLAYWRIGHT_OUTPUT_DIR": str(output_dir),
                "PLAYWRIGHT_JSON_OUTPUT_FILE": str(report),
            }
        )
        command = [
            "pnpm",
            "exec",
            "playwright",
            "test",
            "--ui",
            "--ui-host",
            "127.0.0.1",
            "--ui-port",
            str(PLAYWRIGHT_UI_PORT),
            "--workers",
            "2",
            "--config",
            str(web_root / "playwright.config.ts"),
        ]

        expected_specs: frozenset[tuple[str, int, int, str]] = frozenset()

        def execute(stopping: Event) -> subprocess.CompletedProcess[bytes]:
            nonlocal expected_specs
            expected_specs = playwright_ui_expected_specs(web_root, discovery_environment)
            try:
                return run_checked(
                    command,
                    cwd=web_root,
                    env=environment,
                    stdout=None,
                    stderr=None,
                    stop_event=stopping,
                )
            except DevInterrupted:
                if playwright_ui_report_passed(report, expected_specs):
                    return subprocess.CompletedProcess(command, 130)
                raise

        result = execute_manual_test(
            item,
            kind="ui",
            sha=sha,
            protocol=protocol,
            command=command,
            report=report,
            execute=execute,
        )
        passed = result.returncode in {0, 130} and playwright_ui_report_passed(
            report, expected_specs
        )
        entry: dict[str, object] = {
            "tested_sha": sha,
            "protocol": protocol,
            "status": "passed" if passed else "failed",
            "exit_code": result.returncode,
            "command": command,
            "report": str(report),
            "finished_at": utc_now(),
        }
        record_test_result(item, kind="ui", entry=entry)
        if not passed:
            raise DevError(f"可视化测试进程失败，报告：{report}")


def logs(item: DevPaths, *, service: str | None, tail: str) -> None:
    require_setup(item, runtime_credentials=False)
    if service == "tilt":
        if not (item.logs / "tilt.log").exists():
            raise DevError("尚无 Tilt 日志")
        print((item.logs / "tilt.log").read_text(encoding="utf-8", errors="replace"), end="")
        return
    arguments = ["logs", "--no-color", "--tail", tail]
    if service:
        arguments.append(service)
    result = run_checked(
        compose_command(
            item, *arguments, source=Path(compose_environment(item)["NVSOP_SOURCE_DIR"])
        ),
        cwd=Path(compose_environment(item)["NVSOP_SOURCE_DIR"]),
        env=compose_environment(item),
        stdout=None,
        stderr=None,
    )
    if result.returncode != 0:
        raise DevError("读取 Compose 日志失败")


def acquire_operation_lock(
    item: DevPaths, *, timeout: float = 60, stop_event: Event | None = None
) -> ExclusiveLock:
    lock = ExclusiveLock(item.operation_lock)
    deadline = time.monotonic() + timeout
    while not lock.acquire(blocking=False):
        if stop_event is not None and stop_event.wait(0.25):
            raise DevInterrupted("收到停止请求")
        if stop_event is None:
            time.sleep(0.25)
        if time.monotonic() >= deadline:
            raise DevError("等待开发实例操作锁超时；未删除正在使用的快照内容")
    return lock


def down(item: DevPaths) -> None:
    require_setup(item, runtime_credentials=False)
    # 测试命令本身可能持有 operation_lock；先取消它，launcher 才能完成收尾。
    cancel_test(item)
    pid = read_pid(item.launcher_pid)
    if pid is not None and pid != os.getpid():
        signal_launcher(item, pid, signal.SIGTERM)
    lock = acquire_operation_lock(item)
    try:
        if pid is not None and not launcher_is_locked(item.launcher_pid):
            item.launcher_pid.unlink(missing_ok=True)
        clean_snapshot_overlays(item)
        stop_tilt(item)
        compose_down(item)
        change_state(item, status="stopped", running_sha=None, test=None)
    finally:
        lock.release()
    print(json.dumps({"stopped": True, "volumes_removed": False}, ensure_ascii=False))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="管理固定 nvsop-dev-main 开发实例")
    root.add_argument("--state-dir", type=Path, help="覆盖默认 .nvsop/dev-main 状态目录")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("setup")
    commands.add_parser("run")
    commands.add_parser("status")
    logs_parser = commands.add_parser("logs")
    logs_parser.add_argument("--service")
    logs_parser.add_argument("--tail", default="200")
    commands.add_parser("refresh")
    commands.add_parser("smoke")
    commands.add_parser("test-ui")
    commands.add_parser("down")
    return root


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    item = paths(state=arguments.state_dir)
    try:
        require_main_checkout(item)
        if arguments.command == "setup":
            setup(item)
        elif arguments.command == "run":
            run_loop(item)
        elif arguments.command == "status":
            return status(item)
        elif arguments.command == "logs":
            logs(item, service=arguments.service, tail=arguments.tail)
        elif arguments.command == "refresh":
            refresh(item)
        elif arguments.command == "smoke":
            run_smoke(item)
        elif arguments.command == "test-ui":
            run_ui(item)
        elif arguments.command == "down":
            down(item)
        else:  # pragma: no cover - argparse 已限制
            raise DevError(f"未知命令：{arguments.command}")
    except KeyboardInterrupt:
        print("dev: 操作已中断", file=sys.stderr)
        return 130
    except DevError as error:
        print(f"dev: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
