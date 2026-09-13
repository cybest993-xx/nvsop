"""固定开发实例手动测试的状态与中断支持。"""

from __future__ import annotations

import contextlib
import os
import signal
from collections.abc import Callable, Iterator
from pathlib import Path
from subprocess import CompletedProcess
from threading import Event

StateChange = Callable[..., object]
Now = Callable[[], str]


def test_state(
    item: object,
    *,
    kind: str,
    sha: str,
    protocol: str,
    report: Path,
    change_state: StateChange,
    now: Now,
) -> None:
    change_state(
        item,
        test={
            "kind": kind,
            "pid": os.getpid(),
            "status": "running",
            "tested_sha": sha,
            "protocol": protocol,
            "report": str(report),
            "started_at": now(),
        },
    )


def record_test_result(
    item: object, *, kind: str, entry: dict[str, object], change_state: StateChange
) -> None:
    change_state(item, test=None, **{f"last_{kind}": entry})


@contextlib.contextmanager
def test_stop_event() -> Iterator[Event]:
    """将显式停止转换为可传播给测试子进程的 stop event。"""
    stopping = Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stopping.set()

    old_int = signal.signal(signal.SIGINT, request_stop)
    old_term = signal.signal(signal.SIGTERM, request_stop)
    try:
        yield stopping
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)


def interrupted_test_entry(
    *,
    sha: str,
    protocol: str,
    command: list[str],
    report: Path,
    now: Now,
    log: Path | None = None,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "tested_sha": sha,
        "protocol": protocol,
        "status": "aborted",
        "exit_code": 130,
        "reason": "KeyboardInterrupt",
        "command": command,
        "report": str(report),
        "finished_at": now(),
    }
    if log is not None:
        entry["log"] = str(log)
    return entry


def failed_test_entry(
    *,
    sha: str,
    protocol: str,
    command: list[str],
    report: Path,
    error: Exception,
    now: Now,
    log: Path | None = None,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "tested_sha": sha,
        "protocol": protocol,
        "status": "failed",
        "exit_code": None,
        "error": str(error),
        "error_type": type(error).__name__,
        "command": command,
        "report": str(report),
        "finished_at": now(),
    }
    if log is not None:
        entry["log"] = str(log)
    return entry


def execute_manual_test(
    item: object,
    *,
    kind: str,
    sha: str,
    protocol: str,
    command: list[str],
    report: Path,
    execute: Callable[[Event], CompletedProcess[bytes]],
    change_state: StateChange,
    now: Now,
    log: Path | None = None,
) -> CompletedProcess[bytes]:
    """统一管理手动测试的状态记录、停止传播和异常结果。"""
    test_state(
        item,
        kind=kind,
        sha=sha,
        protocol=protocol,
        report=report,
        change_state=change_state,
        now=now,
    )
    try:
        with test_stop_event() as stopping:
            return execute(stopping)
    except KeyboardInterrupt:
        record_test_result(
            item,
            kind=kind,
            entry=interrupted_test_entry(
                sha=sha, protocol=protocol, command=command, report=report, now=now, log=log
            ),
            change_state=change_state,
        )
        raise
    except Exception as error:
        record_test_result(
            item,
            kind=kind,
            entry=failed_test_entry(
                sha=sha,
                protocol=protocol,
                command=command,
                report=report,
                error=error,
                now=now,
                log=log,
            ),
            change_state=change_state,
        )
        raise
