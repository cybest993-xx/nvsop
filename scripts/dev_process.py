"""固定开发实例的进程识别与信号基础设施。"""

from __future__ import annotations

import os
import signal
from pathlib import Path


def process_alive(pid: int) -> bool:
    """判断 PID 是否仍存在。"""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def process_matches(root: Path, pid: int, *, command_name: str) -> bool:
    """在 Linux 上按脚本路径、工作目录和命令参数校验目标进程。"""
    if os.name == "nt":  # pragma: no cover - 目标开发环境是 WSL2
        return True
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        working_directory = Path(f"/proc/{pid}/cwd").resolve()
    except OSError:
        return False
    arguments = [value.decode("utf-8", errors="replace") for value in command if value]
    if command_name not in arguments:
        return False
    script = root / "scripts" / "dev.py"
    return any(
        value in {str(script), "scripts/dev.py", "./scripts/dev.py"}
        or (value.endswith("/scripts/dev.py") and working_directory == root)
        for value in arguments
    )


def signal_process(pid: int, signum: signal.Signals) -> bool:
    """优先使用 pidfd，避免 PID 重用时误杀其他进程。"""
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is not None and pidfd_send_signal is not None:
        try:
            descriptor = pidfd_open(pid)
        except OSError:
            return False
        try:
            pidfd_send_signal(descriptor, signum)
        except OSError:
            return False
        finally:
            os.close(descriptor)
        return True
    try:  # pragma: no cover - Linux Python 3.11 提供 pidfd
        os.kill(pid, signum)
    except OSError:
        return False
    return True
