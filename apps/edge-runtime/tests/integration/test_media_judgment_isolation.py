"""SYS-34-16 软件媒体路径与判定隔离证据 (Issue #433)。

媒体进程属于推理机本机软件路径, 判定核心只吃归一化观测。本文件在真实
``AutonomousRuntime.run_forever`` 组合上证明: 凭据慢读、媒体进程启动失败/退避重试、
以及运行中进程断连后的恢复尝试都不会等待、改写或伪造判定, 也不会把媒体异常传进工位线程。
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from threading import Event
from time import monotonic
from typing import cast
from unittest.mock import patch
from uuid import UUID

from edge_runtime.judgment import ReasonCode, Verdict
from edge_runtime.judgment.evidence import EvidenceMargins
from edge_runtime.judgment.model import (
    Decision,
    HostInstant,
    Lifecycle,
    Ordering,
    RuntimeParameters,
    Template,
)
from edge_runtime.local_state import LocalState, PendingReport, StationStore
from edge_runtime.local_state.store import open_local_state
from edge_runtime.media import (
    LocalMediaCamera,
    MediaPathMode,
    MediaRuntime,
    MediaRuntimeConfiguration,
    RecordingMode,
)
from edge_runtime.media_retention import read_applied_window
from edge_runtime.runtime import (
    AutonomousRuntime,
    AutonomousStation,
    ConnectionTestCommandLoop,
)
from edge_runtime.station_runtime import InputWaitExpired, StationInputSource
from edge_runtime.supervisor.inputs import ActionRecognized, SupervisorInput
from edge_runtime.supervisor.startup import resume_station

CAMERA_ID = "018f0000-0000-7000-8000-000000000001"
HOST_ID = "018f0000-0000-7000-8000-000000000010"
STATION_ID = "018f0000-0000-7000-8000-000000000020"
MEDIA_PATH = f"camera-{UUID(CAMERA_ID).hex}"
MEDIAMTX_BINARY = Path("/usr/local/bin/mediamtx")
FFMPEG_BINARY = Path("/usr/bin/ffmpeg")

# 独立于实现的期望: start→finish 的完整 SOP 以 PASS 结案, 无理由、无违规、无媒体健康伪造。
EXPECTED_IDENTITY = (Verdict.PASS, (), (), Lifecycle.CLOSED_BY_COMPLETE_SET)


def _identity(decision: Decision) -> tuple[object, ...]:
    return (decision.verdict, decision.reasons, decision.violations, decision.lifecycle)


class _AliveProcess:
    """启动成功且保持存活的本机媒体进程替身。"""

    def __init__(self) -> None:
        self._stopped = False

    def poll(self) -> int | None:
        return 0 if self._stopped else None

    def terminate(self) -> None:
        self._stopped = True

    def kill(self) -> None:
        self._stopped = True

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self._stopped = True
        return 0


class _ExitedProcess:
    """启动即退出, 用于复现媒体进程启动失败。"""

    def __init__(self, code: int = 1) -> None:
        self._code = code

    def poll(self) -> int | None:
        return self._code

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self._code


class _AliveProcessFactory:
    """只替身外部进程工厂; 记录每次真实 Popen 参数。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: list[str], **_kwargs: object) -> _AliveProcess:
        self.calls.append(tuple(args))
        return _AliveProcess()


class _PersistentStartupFailures:
    """让指定二进制每次启动都失败, 第二次失败后放行观测。"""

    def __init__(self, *, failing_binary: Path) -> None:
        self._failing_binary = str(failing_binary)
        self.calls: list[tuple[str, ...]] = []
        self.failure_count = 0
        self.failure_instants: list[float] = []
        self.failures_reached = Event()
        self._lock = threading.Lock()

    def __call__(self, args: list[str], **_kwargs: object) -> _AliveProcess | _ExitedProcess:
        with self._lock:
            self.calls.append(tuple(args))
            if args[0] != self._failing_binary:
                return _AliveProcess()
            self.failure_count += 1
            self.failure_instants.append(monotonic())
            if self.failure_count >= 2:
                self.failures_reached.set()
        return _ExitedProcess()


class _DisconnectingProcessFactory:
    """合成外部进程工厂: 首次启动返回存活替身, 进程被外部终止后的恢复重试返回已退出替身。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.process: _AliveProcess | None = None
        self.recovery_started = Event()

    def __call__(self, args: list[str], **_kwargs: object) -> _AliveProcess | _ExitedProcess:
        del args
        with self._lock:
            if self.process is None:
                self.process = _AliveProcess()
                return self.process
            self.recovery_started.set()
            return _ExitedProcess()


class _IdleCommandLoop:
    """工位循环不依赖中心链路; 命令循环只保持运行到停机。"""

    def run_forever(self, *, should_stop: Callable[[], bool]) -> None:
        while not should_stop():
            time.sleep(0.001)


class _TwoStepInputSource:
    """先给完整 SOP 观测, 之后有界等待, 不阻塞停机。"""

    def __init__(self, *, gate: Event | None = None) -> None:
        self._gate = gate
        self._released = gate is None
        self._inputs: list[SupervisorInput] = [
            ActionRecognized(
                signal="(1) start",
                at=HostInstant(1.0),
                source_time=1.0,
                source_anchor=1.0,
            ),
            ActionRecognized(
                signal="(2) finish",
                at=HostInstant(2.0),
                source_time=2.0,
                source_anchor=1.0,
            ),
        ]
        self.closed = False

    @property
    def ended(self) -> bool:
        return False

    def next_input(self, *, timeout: float | None) -> SupervisorInput | InputWaitExpired:
        if not self._released:
            if self._gate is None or not self._gate.wait(5.0):
                return InputWaitExpired()
            self._released = True
        if self._inputs:
            return self._inputs.pop(0)
        time.sleep(0.02)
        return InputWaitExpired()

    def close(self) -> None:
        self.closed = True


class MediaJudgmentIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.template = Template(
            steps=("(1) start", "(2) finish"),
            ordering=Ordering.ORDERED,
            start_signal="(1) start",
        )
        self.parameters = RuntimeParameters(idle_timeout=10.0, step_deadline=10.0)
        self.margins = EvidenceMargins(leading=0.0, trailing=0.0)

    def _write_readonly_credentials(self, root: Path) -> tuple[Path, Path]:
        username = root / "camera-username"
        password = root / "camera-password"
        username.write_text("synthetic-user\n", encoding="utf-8")
        password.write_text("synthetic-secret\n", encoding="utf-8")  # pragma: allowlist secret
        username.chmod(0o400)
        password.chmod(0o400)
        return username, password

    def _camera(
        self,
        *,
        username: Path,
        password: Path,
        media_path_mode: MediaPathMode,
        recording_mode: RecordingMode,
    ) -> LocalMediaCamera:
        return LocalMediaCamera(
            camera_id=CAMERA_ID,
            camera_name="隔离相机",
            camera_address="10.0.8.21",
            sub_stream_path="/Streaming/Channels/102",
            camera_status="active",
            station_id=STATION_ID,
            station_status="active",
            host_id=HOST_ID,
            media_path=MEDIA_PATH,
            media_path_mode=media_path_mode,
            recording_mode=recording_mode,
            sop_execution=False,
            credentials_configured=True,
            username_file=username,
            password_file=password,
        )

    def _media_configuration(
        self,
        root: Path,
        *,
        username: Path,
        password: Path,
        media_path_mode: MediaPathMode,
        recording_mode: RecordingMode,
    ) -> MediaRuntimeConfiguration:
        return MediaRuntimeConfiguration(
            host_id=HOST_ID,
            host_status="active",
            mediamtx_address=None,
            mediamtx_playback_address=None,
            recording_window_seconds=3600,
            media_config_path=root / "mediamtx.yml",
            recording_directory=root / "recordings",
            mediamtx_binary=MEDIAMTX_BINARY,
            ffmpeg_binary=FFMPEG_BINARY,
            rtsp_bind_address=":8554",
            webrtc_bind_address=":8889",
            webrtc_udp_bind_address=":8189",
            playback_bind_address=":9996",
            allow_origins=("https://control.example.test",),
            record_segment_duration_seconds=60,
            preview_release_delay_seconds=5,
            startup_timeout_seconds=0.1,
            transcode_threads=2,
            recording_window_confirmation=None,
            cameras=(
                self._camera(
                    username=username,
                    password=password,
                    media_path_mode=media_path_mode,
                    recording_mode=recording_mode,
                ),
            ),
        )

    def _build_runtime(
        self,
        state: LocalState,
        store: StationStore,
        source: StationInputSource,
        *,
        media: MediaRuntime | None,
    ) -> AutonomousRuntime:
        supervisor = resume_station(
            store,
            template=self.template,
            parameters=self.parameters,
            margins=self.margins,
        )
        station = AutonomousStation(supervisor=supervisor, source=source)
        return AutonomousRuntime(
            command_loop=cast(ConnectionTestCommandLoop, _IdleCommandLoop()),
            stations=(station,),
            state=state,
            media=media,
        )

    def _wait_for_reports(
        self, store: StationStore, *, timeout: float = 10.0
    ) -> tuple[PendingReport, ...]:
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            reports = store.pending_reports()
            if reports:
                return reports
            time.sleep(0.01)
        self.fail("durable report was not persisted before the timeout")

    def _wait_for_applied_window(
        self, media_config_path: Path, expected: int, *, timeout: float = 10.0
    ) -> None:
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            if read_applied_window(media_config_path) == expected:
                return
            time.sleep(0.01)
        self.fail("media runtime never recorded a successful startup")

    def _wait_for_media_error(self, media: MediaRuntime, *, timeout: float = 10.0) -> None:
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            if media.last_error is not None:
                return
            time.sleep(0.01)
        self.fail("media recovery failure was not observed")

    def _baseline_identity(self, database: Path) -> tuple[object, ...]:
        """无媒体路径的参照判定: 唯一变量是媒体, 判定身份必须不变。"""
        state = open_local_state(str(database))
        try:
            store = state.station(STATION_ID)
            runtime = self._build_runtime(state, store, _TwoStepInputSource(), media=None)
            captured: list[Decision] = []
            timed_out = Event()
            deadline = monotonic() + 10.0

            def should_stop() -> bool:
                reports = store.pending_reports()
                if reports and not captured:
                    captured.append(reports[0].decision)
                if reports:
                    return True
                if monotonic() > deadline:
                    timed_out.set()
                    return True
                return False

            runtime.run_forever(should_stop=should_stop)
            self.assertFalse(timed_out.is_set(), "baseline run did not produce a report in time")
            self.assertEqual(1, len(captured))
            identity = _identity(captured[0])
            self.assertEqual(EXPECTED_IDENTITY, identity)
            return identity
        finally:
            state.close()

    def test_slow_credentials_read_does_not_block_or_change_judgment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = self._baseline_identity(root / "baseline.sqlite")
            self.assertEqual(EXPECTED_IDENTITY, baseline)

            username, password = self._write_readonly_credentials(root)
            media_configuration = self._media_configuration(
                root,
                username=username,
                password=password,
                media_path_mode=MediaPathMode.PASSTHROUGH,
                recording_mode=RecordingMode.CONTINUOUS,
            )
            read_started = Event()
            release_read = Event()
            read_finished = Event()
            read_timed_out = Event()
            real_read_text = Path.read_text

            def blocking_read_text(
                path: Path, encoding: str | None = None, errors: str | None = None
            ) -> str:
                if path in (username, password):
                    read_started.set()
                    if release_read.wait(10.0):
                        read_finished.set()
                    else:
                        read_timed_out.set()
                return real_read_text(path, encoding=encoding, errors=errors)

            factory = _AliveProcessFactory()
            media = MediaRuntime(media_configuration, popen=factory)
            state = open_local_state(str(root / "blocked.sqlite"))
            self.addCleanup(state.close)
            store = state.station(STATION_ID)
            # 观测只在媒体线程真正卡进凭据读取之后才放行: 判定不可能在慢读之前完成。
            source = _TwoStepInputSource(gate=read_started)
            runtime = self._build_runtime(state, store, source, media=media)
            stopping = Event()
            errors: list[BaseException] = []

            def run() -> None:
                try:
                    runtime.run_forever(should_stop=stopping.is_set)
                except BaseException as error:
                    errors.append(error)

            worker = threading.Thread(target=run, daemon=True)
            with patch.object(Path, "read_text", blocking_read_text):
                worker.start()
                try:
                    self.assertTrue(read_started.wait(5.0), "媒体线程从未进入凭据读取")
                    reports = self._wait_for_reports(store)
                    # 媒体仍卡在只读凭据读取上: 没有任何进程被启动。
                    self.assertEqual([], factory.calls)
                    self.assertIsNone(media.last_error)
                    self.assertFalse(read_timed_out.is_set())
                    self.assertFalse(release_read.is_set())
                    self.assertFalse(read_finished.is_set())
                    self.assertTrue(worker.is_alive())
                    self.assertEqual([], errors)
                    self.assertEqual(1, len(reports))
                    report = reports[0]
                    self.assertEqual(baseline, _identity(report.decision))
                    self.assertIsNot(report.decision.verdict, Verdict.FAIL)
                    self.assertNotIn(ReasonCode.STREAM_LOST, report.decision.reasons)
                    # 媒体路径不产生也不伪造流健康事实。
                    self.assertEqual((), store.pending_health_reports())
                finally:
                    release_read.set()
                    stopping.set()
                    worker.join(timeout=10.0)
            self.assertFalse(worker.is_alive())
            self.assertEqual([], errors)
            self.assertTrue(source.closed)

    def test_persistent_media_startup_failure_retries_without_touching_judgment(self) -> None:
        for failing_binary, media_path_mode, recording_mode in (
            (MEDIAMTX_BINARY, MediaPathMode.PASSTHROUGH, RecordingMode.CONTINUOUS),
            (FFMPEG_BINARY, MediaPathMode.CPU_TRANSCODE, RecordingMode.CONTINUOUS),
        ):
            with self.subTest(failing_binary=failing_binary.name):
                self._run_persistent_failure_case(
                    failing_binary=failing_binary,
                    media_path_mode=media_path_mode,
                    recording_mode=recording_mode,
                )

    def _run_persistent_failure_case(
        self,
        *,
        failing_binary: Path,
        media_path_mode: MediaPathMode,
        recording_mode: RecordingMode,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            username, password = self._write_readonly_credentials(root)
            media_configuration = self._media_configuration(
                root,
                username=username,
                password=password,
                media_path_mode=media_path_mode,
                recording_mode=recording_mode,
            )
            factory = _PersistentStartupFailures(failing_binary=failing_binary)
            media = MediaRuntime(media_configuration, popen=factory)
            state = open_local_state(str(root / "failure.sqlite"))
            self.addCleanup(state.close)
            store = state.station(STATION_ID)
            # 观测只在媒体连续两次启动失败之后放行, 证明判定发生在重试之后而非之前。
            source = _TwoStepInputSource(gate=factory.failures_reached)
            runtime = self._build_runtime(state, store, source, media=media)
            stopping = Event()
            errors: list[BaseException] = []

            def run() -> None:
                try:
                    runtime.run_forever(should_stop=stopping.is_set)
                except BaseException as error:
                    errors.append(error)

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            try:
                reports = self._wait_for_reports(store)
                self.assertEqual(1, len(reports))
                self.assertEqual(EXPECTED_IDENTITY, _identity(reports[0].decision))
                self.assertIsNot(reports[0].decision.verdict, Verdict.FAIL)
                self.assertEqual((), store.pending_health_reports())
            finally:
                stopping.set()
                worker.join(timeout=10.0)
            self.assertFalse(worker.is_alive())
            self.assertEqual([], errors)
            self.assertTrue(source.closed)
            # 媒体异常被本机运行时吸收, 没有传进工位线程; 退避重试至少两次。
            self.assertGreaterEqual(factory.failure_count, 2)
            # 真实 0.5s 退避: 前两次启动尝试之间必须留出重试间隔, 而非忙循环。
            self.assertGreaterEqual(len(factory.failure_instants), 2)
            self.assertGreaterEqual(factory.failure_instants[1] - factory.failure_instants[0], 0.45)
            self.assertIsNotNone(media.last_error)

    def test_media_process_disconnect_retries_without_touching_judgment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            username, password = self._write_readonly_credentials(root)
            media_configuration = self._media_configuration(
                root,
                username=username,
                password=password,
                media_path_mode=MediaPathMode.PASSTHROUGH,
                recording_mode=RecordingMode.CONTINUOUS,
            )
            factory = _DisconnectingProcessFactory()
            media = MediaRuntime(media_configuration, popen=factory)
            state = open_local_state(str(root / "disconnect.sqlite"))
            self.addCleanup(state.close)
            store = state.station(STATION_ID)
            # 观测只在进程被外部终止并触发一次恢复尝试之后放行。
            source = _TwoStepInputSource(gate=factory.recovery_started)
            runtime = self._build_runtime(state, store, source, media=media)
            stopping = Event()
            errors: list[BaseException] = []

            def run() -> None:
                try:
                    runtime.run_forever(should_stop=stopping.is_set)
                except BaseException as error:
                    errors.append(error)

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            try:
                self._wait_for_applied_window(
                    media_configuration.media_config_path,
                    media_configuration.recording_window_seconds,
                )
                self.assertEqual((), store.pending_reports())
                started = factory.process
                assert started is not None, "媒体进程从未启动"
                started.terminate()
                self.assertTrue(
                    factory.recovery_started.wait(5.0),
                    "媒体进程断连后从未触发恢复重试",
                )
                reports = self._wait_for_reports(store)
                self.assertEqual(1, len(reports))
                self.assertEqual(EXPECTED_IDENTITY, _identity(reports[0].decision))
                self.assertIsNot(reports[0].decision.verdict, Verdict.FAIL)
                self.assertEqual((), store.pending_health_reports())
                self._wait_for_media_error(media)
            finally:
                stopping.set()
                worker.join(timeout=10.0)
            self.assertFalse(worker.is_alive())
            self.assertEqual([], errors)
            self.assertTrue(source.closed)


if __name__ == "__main__":
    unittest.main()
