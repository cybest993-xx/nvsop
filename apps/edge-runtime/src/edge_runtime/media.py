"""推理机拥有的 MediaMTX/FFmpeg 媒体路径: 只读取本机 secret, 不让浏览器或中心进入此进程。"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from threading import Event, RLock, Thread, current_thread
from time import monotonic, sleep
from typing import Protocol, TypeVar
from urllib.parse import quote, urlsplit
from uuid import UUID

from edge_runtime.configuration_values import (
    _array,
    _object,
    _positive_number,
    _require_keys,
)
from edge_runtime.configuration_values import (
    _non_empty_string as _string,
)
from edge_runtime.media_retention import (
    RecordingImpact,
    estimate_recording_impact,
    parse_recording_impact,
    read_applied_window,
    same_impact,
    write_applied_window,
)


class MediaPathMode(StrEnum):
    PASSTHROUGH = "passthrough"
    CPU_TRANSCODE = "cpu_transcode"


class RecordingMode(StrEnum):
    PREVIEW_ONLY = "preview_only"
    CONTINUOUS = "continuous"


@dataclass(frozen=True, slots=True)
class LocalMediaCamera:
    camera_id: str
    camera_name: str
    camera_address: str
    sub_stream_path: str
    camera_status: str
    station_id: str
    station_status: str
    host_id: str
    media_path: str
    media_path_mode: MediaPathMode
    recording_mode: RecordingMode
    sop_execution: bool
    credentials_configured: bool
    username_file: Path | None
    password_file: Path | None


@dataclass(frozen=True, slots=True)
class MediaRuntimeConfiguration:
    host_id: str
    host_status: str
    mediamtx_address: str | None
    mediamtx_playback_address: str | None
    recording_window_seconds: int
    media_config_path: Path
    recording_directory: Path
    mediamtx_binary: Path
    ffmpeg_binary: Path
    rtsp_bind_address: str
    webrtc_bind_address: str
    webrtc_udp_bind_address: str
    playback_bind_address: str
    allow_origins: tuple[str, ...]
    record_segment_duration_seconds: int
    preview_release_delay_seconds: int
    startup_timeout_seconds: float
    transcode_threads: int
    recording_window_confirmation: RecordingImpact | None
    cameras: tuple[LocalMediaCamera, ...]


class _Process(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


def load_media_runtime_configuration(value: object) -> MediaRuntimeConfiguration:
    """解析严格的本机媒体配置; 未知字段直接拒绝。"""
    config = _object(value, "media configuration")
    _require_keys(
        config,
        required={
            "host_id",
            "host_status",
            "mediamtx_address",
            "mediamtx_playback_address",
            "recording_window_seconds",
            "media_config_path",
            "recording_directory",
            "mediamtx_binary",
            "ffmpeg_binary",
            "rtsp_bind_address",
            "webrtc_bind_address",
            "webrtc_udp_bind_address",
            "playback_bind_address",
            "allow_origins",
            "record_segment_duration_seconds",
            "preview_release_delay_seconds",
            "startup_timeout_seconds",
            "transcode_threads",
            "cameras",
        },
        optional={"recording_window_confirmation"},
    )
    cameras = tuple(
        _camera(item, host_id=_string(config["host_id"], "host_id"))
        for item in _array(config["cameras"], "cameras")
    )
    _validate_cameras(cameras)
    recording_window = _positive_integer(
        config["recording_window_seconds"], "recording_window_seconds"
    )
    segment = _positive_integer(
        config["record_segment_duration_seconds"], "record_segment_duration_seconds"
    )
    if segment > recording_window:
        raise ValueError("record_segment_duration_seconds must not exceed recording_window_seconds")
    release_delay = _non_negative_integer(
        config["preview_release_delay_seconds"], "preview_release_delay_seconds"
    )
    startup_timeout = _positive_number(config["startup_timeout_seconds"], "startup_timeout_seconds")
    transcode_threads = _positive_integer(config["transcode_threads"], "transcode_threads")
    confirmation = (
        parse_recording_impact(config["recording_window_confirmation"])
        if "recording_window_confirmation" in config
        else None
    )
    allow_origins = tuple(
        _origin(item, "allow_origins item")
        for item in _array(config["allow_origins"], "allow_origins")
    )
    if not allow_origins:
        raise ValueError("allow_origins must not be empty")
    configuration = MediaRuntimeConfiguration(
        host_id=_string(config["host_id"], "host_id"),
        host_status=_status(config["host_status"], "host_status"),
        mediamtx_address=_optional_http_url(config["mediamtx_address"], "mediamtx_address"),
        mediamtx_playback_address=_optional_http_url(
            config["mediamtx_playback_address"], "mediamtx_playback_address"
        ),
        recording_window_seconds=recording_window,
        media_config_path=_path(config["media_config_path"], "media_config_path"),
        recording_directory=_path(config["recording_directory"], "recording_directory"),
        mediamtx_binary=_path(config["mediamtx_binary"], "mediamtx_binary"),
        ffmpeg_binary=_path(config["ffmpeg_binary"], "ffmpeg_binary"),
        rtsp_bind_address=_bind_address(config["rtsp_bind_address"], "rtsp_bind_address"),
        webrtc_bind_address=_bind_address(config["webrtc_bind_address"], "webrtc_bind_address"),
        webrtc_udp_bind_address=_bind_address(
            config["webrtc_udp_bind_address"], "webrtc_udp_bind_address"
        ),
        playback_bind_address=_bind_address(
            config["playback_bind_address"], "playback_bind_address"
        ),
        allow_origins=allow_origins,
        record_segment_duration_seconds=segment,
        preview_release_delay_seconds=release_delay,
        startup_timeout_seconds=startup_timeout,
        transcode_threads=transcode_threads,
        recording_window_confirmation=confirmation,
        cameras=cameras,
    )
    _validate_configuration(configuration)
    return configuration


def validate_sop_camera_bindings(
    configuration: MediaRuntimeConfiguration,
    station_ids: set[str],
    confirmed_camera_ids: set[str],
) -> None:
    """拒绝未被中心确认或不符合 SOP 录像约束的本地相机。"""
    if any(camera.camera_id not in confirmed_camera_ids for camera in configuration.cameras):
        raise ValueError("media cameras must belong to the confirmed configuration")
    if any(
        not camera.sop_execution and camera.station_id in station_ids
        for camera in configuration.cameras
    ):
        raise ValueError("SOP stations cannot use media cameras marked as non-SOP")


def render_mediamtx_config(
    configuration: MediaRuntimeConfiguration,
    *,
    secret_reader: SecretReader | None = None,
) -> str:
    """生成完整的 MediaMTX v1 配置, 不执行 shell 命令。"""
    _validate_configuration(configuration)
    reader = _read_secret if secret_reader is None else secret_reader
    lines = [
        "logLevel: info",
        f"rtspAddress: {_yaml_string(configuration.rtsp_bind_address)}",
        f"webrtcAddress: {_yaml_string(configuration.webrtc_bind_address)}",
        f"webrtcLocalUDPAddress: {_yaml_string(configuration.webrtc_udp_bind_address)}",
        f"webrtcAllowOrigins: {_yaml_strings(configuration.allow_origins)}",
        "playback: yes",
        f"playbackAddress: {_yaml_string(configuration.playback_bind_address)}",
        f"playbackAllowOrigins: {_yaml_strings(configuration.allow_origins)}",
        "pathDefaults:",
        "  recordFormat: fmp4",
        f"  recordPath: {_yaml_string(_record_path(configuration.recording_directory))}",
        f"  recordSegmentDuration: {_duration(configuration.record_segment_duration_seconds)}",
        f"  recordDeleteAfter: {_duration(configuration.recording_window_seconds)}",
        "paths:",
    ]
    active_host = configuration.host_status == "active"
    for camera in configuration.cameras:
        enabled = (
            active_host and camera.camera_status == "active" and camera.station_status == "active"
        )
        if enabled:
            try:
                source = _source_url(camera, secret_reader=reader)
                lines.extend(_render_camera_path(configuration, camera, source))
            except BaseException as error:
                raise ValueError(
                    f"camera {camera.camera_id} media source is invalid: {error}"
                ) from error
        else:
            # 保留 path 配置让 MediaMTX 在重启后仍能查询既有分段, 但不重新取流或录制。
            lines.extend(_render_disabled_camera_path(camera))
    return "\n".join(lines) + "\n"


class SecretReader(Protocol):
    def __call__(self, path: Path, name: str) -> str: ...


def _render_camera_path(
    configuration: MediaRuntimeConfiguration,
    camera: LocalMediaCamera,
    source: str,
) -> list[str]:
    record = camera.recording_mode is RecordingMode.CONTINUOUS
    lines = [f"  {_yaml_key(camera.media_path)}:"]
    if camera.media_path_mode is MediaPathMode.PASSTHROUGH:
        lines.append(f"    source: {_yaml_string(source)}")
        if not record:
            lines.extend(
                [
                    "    sourceOnDemand: yes",
                    "    sourceOnDemandStartTimeout: "
                    f"{_seconds(configuration.startup_timeout_seconds)}",
                    "    sourceOnDemandCloseAfter: "
                    f"{_duration(configuration.preview_release_delay_seconds)}",
                ]
            )
    else:
        lines.append("    source: publisher")
        if not record:
            command = _ffmpeg_command(configuration, camera, source)
            lines.extend(
                [
                    f"    runOnDemand: {_yaml_string(shlex.join(command))}",
                    "    runOnDemandRestart: yes",
                    "    runOnDemandStartTimeout: "
                    f"{_seconds(configuration.startup_timeout_seconds)}",
                    "    runOnDemandCloseAfter: "
                    f"{_duration(configuration.preview_release_delay_seconds)}",
                ]
            )
    lines.append(f"    record: {'yes' if record else 'no'}")
    return lines


def _render_disabled_camera_path(camera: LocalMediaCamera) -> list[str]:
    return [f"  {_yaml_key(camera.media_path)}:", "    source: publisher", "    record: no"]


def _ffmpeg_command(
    configuration: MediaRuntimeConfiguration,
    camera: LocalMediaCamera,
    source: str,
) -> list[str]:
    return [
        str(configuration.ffmpeg_binary),
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-i",
        source,
        "-map",
        "0:v:0",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-tune",
        "zerolatency",
        "-g",
        "30",
        "-threads",
        str(configuration.transcode_threads),
        "-f",
        "rtsp",
        "-rtsp_transport",
        "tcp",
        f"rtsp://127.0.0.1:{_bind_port(configuration.rtsp_bind_address)}/{camera.media_path}",
    ]


def ffmpeg_command_for_camera(
    configuration: MediaRuntimeConfiguration,
    camera: LocalMediaCamera,
    *,
    secret_reader: SecretReader | None = None,
) -> tuple[str, ...]:
    """返回连续 CPU 路径的 argv, 调用方直接传给 Popen。"""
    if camera.media_path_mode is not MediaPathMode.CPU_TRANSCODE:
        raise ValueError("ffmpeg is only used for cpu_transcode routes")
    if camera.recording_mode is not RecordingMode.CONTINUOUS:
        raise ValueError("preview_only routes are started by MediaMTX runOnDemand")
    reader = _read_secret if secret_reader is None else secret_reader
    return tuple(
        _ffmpeg_command(
            configuration,
            camera,
            _source_url(camera, secret_reader=reader),
        )
    )


class MediaRuntime:
    """原子应用本机媒体拓扑并拥有其 MediaMTX/FFmpeg 进程。"""

    def __init__(
        self,
        configuration: MediaRuntimeConfiguration,
        *,
        popen: Callable[..., _Process] = subprocess.Popen,
    ) -> None:
        _validate_configuration(configuration)
        self._configuration = configuration
        self._popen = popen
        self._mediamtx: _Process | None = None
        self._ffmpeg: list[_Process] = []
        self._started = False
        self._applied_window = read_applied_window(configuration.media_config_path)
        self._lock = RLock()
        self._watch_stop: Event | None = None
        self._watch_thread: Thread | None = None
        self._last_error: str | None = None

    @property
    def configuration(self) -> MediaRuntimeConfiguration:
        return self._configuration

    @property
    def last_error(self) -> str | None:
        """返回最近一次媒体进程恢复失败的原因。"""
        return self._last_error

    def start(self) -> None:
        try:
            self.apply(self._configuration)
        except BaseException as error:
            self._last_error = str(error)
            raise

    def apply(self, configuration: MediaRuntimeConfiguration) -> None:
        """校验并替换媒体路由; 候选失败时恢复上一份有效路由。"""
        _validate_configuration(configuration)
        self._stop_watcher()
        with self._lock:
            was_started = self._started
            if self._started and configuration == self._configuration:
                self._start_watcher()
                return
            previous_window = self._applied_window
            if self._started:
                previous_window = self._configuration.recording_window_seconds
            try:
                _require_recording_confirmation(configuration, previous_window)
                rendered = render_mediamtx_config(configuration)
                old_configuration = self._configuration if self._started else None
                old_rendered = (
                    render_mediamtx_config(old_configuration)
                    if old_configuration is not None
                    else None
                )
            except BaseException:
                if was_started:
                    self._start_watcher()
                raise
            try:
                self._stop_processes()
                self._launch(configuration, rendered)
            except BaseException:
                try:
                    if old_configuration is not None and old_rendered is not None:
                        try:
                            self._launch(old_configuration, old_rendered)
                        except BaseException as rollback_error:
                            raise RuntimeError(
                                "media candidate failed and rollback failed"
                            ) from rollback_error
                    raise
                finally:
                    if old_configuration is not None:
                        self._start_watcher()
            self._configuration = configuration
            self._last_error = None
            self._start_watcher()

    def stop(self) -> None:
        self._stop_watcher()
        with self._lock:
            self._stop_processes()

    def close(self) -> None:
        self.stop()

    def _stop_processes(self) -> None:
        for process in reversed(self._ffmpeg):
            _stop_process(process)
        self._ffmpeg.clear()
        _stop_process(self._mediamtx)
        self._mediamtx = None
        self._started = False

    def _launch(self, configuration: MediaRuntimeConfiguration, rendered: str) -> None:
        if configuration.host_status != "active":
            _write_atomic(configuration.media_config_path, rendered)
            write_applied_window(
                configuration.media_config_path, configuration.recording_window_seconds
            )
            self._configuration = configuration
            self._applied_window = configuration.recording_window_seconds
            self._started = True
            return
        configuration.recording_directory.mkdir(parents=True, exist_ok=True)
        temporary = _write_temporary(configuration.media_config_path, rendered)
        mediamtx: _Process | None = None
        ffmpeg: list[_Process] = []
        try:
            mediamtx = self._popen(
                [str(configuration.mediamtx_binary), str(temporary)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _wait_started(mediamtx, configuration.startup_timeout_seconds)
            for camera in configuration.cameras:
                if (
                    camera.camera_status == "active"
                    and camera.station_status == "active"
                    and camera.media_path_mode is MediaPathMode.CPU_TRANSCODE
                    and camera.recording_mode is RecordingMode.CONTINUOUS
                ):
                    try:
                        ffmpeg_process = self._popen(
                            list(ffmpeg_command_for_camera(configuration, camera)),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        _wait_started(ffmpeg_process, configuration.startup_timeout_seconds)
                    except BaseException as error:
                        raise RuntimeError(
                            f"camera {camera.camera_id} CPU transcode failed: {error}"
                        ) from error
                    ffmpeg.append(ffmpeg_process)
            _write_atomic(configuration.media_config_path, rendered, source=temporary)
            write_applied_window(
                configuration.media_config_path, configuration.recording_window_seconds
            )
        except BaseException:
            for process in reversed(ffmpeg):
                _stop_process(process)
            _stop_process(mediamtx)
            temporary.unlink(missing_ok=True)
            raise
        self._mediamtx = mediamtx
        self._ffmpeg = ffmpeg
        self._applied_window = configuration.recording_window_seconds
        self._started = True

    def _start_watcher(self) -> None:
        if self._configuration.host_status != "active":
            return
        stop = Event()
        self._watch_stop = stop
        self._watch_thread = Thread(
            target=self._watch_processes,
            args=(stop,),
            name="edge-media-watch",
            daemon=True,
        )
        self._watch_thread.start()

    def _stop_watcher(self) -> None:
        stop = self._watch_stop
        thread = self._watch_thread
        self._watch_stop = None
        self._watch_thread = None
        if stop is None:
            return
        stop.set()
        if thread is not None and thread is not current_thread():
            thread.join()

    def _watch_processes(self, stop: Event) -> None:
        interval = min(max(self._configuration.startup_timeout_seconds / 10, 0.1), 1.0)
        while not stop.wait(interval):
            with self._lock:
                if stop.is_set():
                    return
                if self._started and all(process.poll() is None for process in self._processes()):
                    continue
                try:
                    rendered = render_mediamtx_config(self._configuration)
                    self._stop_processes()
                    self._launch(self._configuration, rendered)
                    self._last_error = None
                except BaseException as error:
                    self._last_error = str(error)

    def _processes(self) -> tuple[_Process, ...]:
        return tuple(process for process in (self._mediamtx, *self._ffmpeg) if process is not None)


def _require_recording_confirmation(
    configuration: MediaRuntimeConfiguration, previous_window: int | None
) -> None:
    """缩短窗口前用真实目录复核候选确认, 拒绝没有影响证据的启动。"""
    target = configuration.recording_window_seconds
    if previous_window is None or target >= previous_window:
        return
    impact = estimate_recording_impact(
        configuration.recording_directory,
        previous_window_seconds=previous_window,
        target_window_seconds=target,
    )
    confirmation = configuration.recording_window_confirmation
    if confirmation is None:
        raise ValueError(
            "recording window shortening needs local impact confirmation: "
            f"{impact.segment_count} segments, {impact.bytes} bytes, "
            f"{impact.oldest_segment_at or 'none'} to {impact.newest_segment_at or 'none'}"
        )
    if not confirmation.confirmed_by:
        raise ValueError("recording window confirmation requires an operator")
    if not same_impact(confirmation, impact):
        raise ValueError("recording window impact changed; estimate it again before confirming")


def _validate_configuration(configuration: MediaRuntimeConfiguration) -> None:
    if not configuration.host_id:
        raise ValueError("host_id must not be empty")
    if configuration.host_status not in {"active", "deactivated"}:
        raise ValueError("host_status is unsupported")
    if configuration.recording_window_seconds <= 0:
        raise ValueError("recording_window_seconds must be positive")
    if configuration.record_segment_duration_seconds <= 0:
        raise ValueError("record_segment_duration_seconds must be positive")
    if configuration.preview_release_delay_seconds < 0:
        raise ValueError("preview_release_delay_seconds must not be negative")
    if configuration.startup_timeout_seconds <= 0:
        raise ValueError("startup_timeout_seconds must be positive")
    if isinstance(configuration.transcode_threads, bool) or configuration.transcode_threads <= 0:
        raise ValueError("transcode_threads must be positive")
    _optional_http_url(configuration.mediamtx_address, "mediamtx_address")
    _optional_http_url(configuration.mediamtx_playback_address, "mediamtx_playback_address")
    if configuration.record_segment_duration_seconds > configuration.recording_window_seconds:
        raise ValueError("record segment duration must not exceed recording window")
    _bind_address(configuration.rtsp_bind_address, "rtsp_bind_address")
    _bind_address(configuration.webrtc_bind_address, "webrtc_bind_address")
    _bind_address(configuration.webrtc_udp_bind_address, "webrtc_udp_bind_address")
    _bind_address(configuration.playback_bind_address, "playback_bind_address")
    _bind_port(configuration.rtsp_bind_address)
    if not configuration.allow_origins:
        raise ValueError("allow_origins must not be empty")
    for origin in configuration.allow_origins:
        _origin(origin, "allow_origins item")
    for path, name in (
        (configuration.media_config_path, "media_config_path"),
        (configuration.recording_directory, "recording_directory"),
        (configuration.mediamtx_binary, "mediamtx_binary"),
        (configuration.ffmpeg_binary, "ffmpeg_binary"),
    ):
        if not path.is_absolute():
            raise ValueError(f"{name} must be an absolute path")
    if not configuration.media_config_path.name:
        raise ValueError("media_config_path must name a file")
    _validate_cameras(configuration.cameras)


def _validate_cameras(cameras: tuple[LocalMediaCamera, ...]) -> None:
    ids: set[str] = set()
    paths: set[str] = set()
    for camera in cameras:
        try:
            camera_uuid = UUID(camera.camera_id)
        except ValueError as error:
            raise ValueError("camera_id must be a UUID") from error
        expected_path = f"camera-{camera_uuid.hex}"
        if camera.media_path != expected_path:
            raise ValueError("media_path must be derived from camera_id")
        try:
            UUID(camera.station_id)
        except ValueError as error:
            raise ValueError("station_id must be a UUID") from error
        if camera.camera_id in ids or camera.media_path in paths:
            raise ValueError("camera identifiers and media paths must be unique")
        ids.add(camera.camera_id)
        paths.add(camera.media_path)
        if camera.camera_status not in {"active", "deactivated"}:
            raise ValueError("camera_status is unsupported")
        if camera.station_status not in {"active", "deactivated"}:
            raise ValueError("station_status is unsupported")
        if camera.sop_execution and camera.recording_mode is RecordingMode.PREVIEW_ONLY:
            raise ValueError("SOP cameras must use continuous recording")
        if camera.host_id == "":
            raise ValueError("camera host_id must not be empty")
        if not camera.camera_name:
            raise ValueError("camera_name must not be empty")
        _camera_address(camera.camera_address)
        _stream_path(camera.sub_stream_path)
        if camera.credentials_configured and (
            camera.username_file is None or camera.password_file is None
        ):
            raise ValueError(
                "configured camera credentials require username_file and password_file"
            )
        for path, name in (
            (camera.username_file, "username_file"),
            (camera.password_file, "password_file"),
        ):
            if path is not None and not path.is_absolute():
                raise ValueError(f"{name} must be an absolute path")


def _source_url(camera: LocalMediaCamera, *, secret_reader: SecretReader) -> str:
    username = password = ""
    if camera.credentials_configured:
        if camera.username_file is None or camera.password_file is None:
            raise ValueError("configured camera credentials require local secret files")
        username = quote(secret_reader(camera.username_file, "camera username"), safe="")
        password = quote(secret_reader(camera.password_file, "camera password"), safe="")
        authority = f"{username}:{password}@{camera.camera_address}"
    else:
        authority = camera.camera_address
    return f"rtsp://{authority}{camera.sub_stream_path}"


def _record_path(directory: Path) -> str:
    return str(directory / "%path" / "%Y-%m-%d_%H-%M-%S-%f")


def _duration(seconds: int) -> str:
    return f"{seconds}s"


def _seconds(value: float) -> str:
    return f"{value:g}s"


def _yaml_key(value: str) -> str:
    if not value or any(
        character
        not in (
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "0123456789-_"  # pragma: allowlist secret
        )
        for character in value
    ):
        raise ValueError("media path is not a safe YAML key")
    return value


def _yaml_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _yaml_strings(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_yaml_string(value) for value in values) + "]"


def _write_temporary(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _write_atomic(path: Path, content: str, *, source: Path | None = None) -> None:
    temporary = source or _write_temporary(path, content)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _wait_started(process: _Process, timeout: float) -> None:
    deadline = monotonic() + timeout
    while process.poll() is None and monotonic() < deadline:
        sleep(min(0.02, max(0.0, deadline - monotonic())))
    code = process.poll()
    if code is not None:
        raise RuntimeError(f"media process exited during startup with code {code}")


def _stop_process(process: _Process | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _camera(value: object, *, host_id: str) -> LocalMediaCamera:
    config = _object(value, "media camera")
    _require_keys(
        config,
        required={
            "camera_id",
            "camera_name",
            "camera_address",
            "sub_stream_path",
            "camera_status",
            "station_id",
            "station_status",
            "host_id",
            "media_path",
            "media_path_mode",
            "recording_mode",
            "sop_execution",
            "credentials_configured",
            "credential_files",
        },
    )
    camera_host_id = _string(config["host_id"], "camera host_id")
    if camera_host_id != host_id:
        raise ValueError("camera host_id does not match media host_id")
    credential_files = _object(config["credential_files"], "credential_files")
    _require_keys(credential_files, required={"username_file", "password_file"})
    configured = _boolean(config["credentials_configured"], "credentials_configured")
    return LocalMediaCamera(
        camera_id=_string(config["camera_id"], "camera_id"),
        camera_name=_string(config["camera_name"], "camera_name"),
        camera_address=_string(config["camera_address"], "camera_address"),
        sub_stream_path=_string(config["sub_stream_path"], "sub_stream_path"),
        camera_status=_status(config["camera_status"], "camera_status"),
        station_id=_uuid(config["station_id"], "station_id"),
        station_status=_status(config["station_status"], "station_status"),
        host_id=camera_host_id,
        media_path=_string(config["media_path"], "media_path"),
        media_path_mode=_enum(config["media_path_mode"], MediaPathMode, "media_path_mode"),
        recording_mode=_enum(config["recording_mode"], RecordingMode, "recording_mode"),
        sop_execution=_boolean(config["sop_execution"], "sop_execution"),
        credentials_configured=configured,
        username_file=_path(credential_files["username_file"], "username_file"),
        password_file=_path(credential_files["password_file"], "password_file"),
    )


def _read_secret(path: Path, name: str) -> str:
    if path.stat().st_mode & 0o222:
        raise ValueError(f"{name} file must be read-only")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"{name} file must not be empty")
    return value


def _uuid(value: object, name: str) -> str:
    text = _string(value, name)
    try:
        UUID(text)
    except ValueError as error:
        raise ValueError(f"{name} must be a UUID") from error
    return text


def _status(value: object, name: str) -> str:
    value = _string(value, name)
    if value not in {"active", "deactivated"}:
        raise ValueError(f"{name} is unsupported")
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


EnumT = TypeVar("EnumT", bound=StrEnum)


def _enum(value: object, enum_type: type[EnumT], name: str) -> EnumT:
    try:
        return enum_type(_string(value, name))
    except ValueError as error:
        raise ValueError(f"{name} is unsupported") from error


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _non_negative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _path(value: object, name: str) -> Path:
    result = Path(_string(value, name))
    if not result.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return result


def _origin(value: object, name: str) -> str:
    result = _string(value, name)
    if any(character.isspace() for character in result):
        raise ValueError(f"{name} must not contain whitespace")
    parsed = urlsplit(result)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError(f"{name} must be an http(s) origin")
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{name} must be a bare http(s) origin")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{name} has an invalid port") from error
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"{name} has an invalid port")
    return result.rstrip("/")


def _optional_http_url(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _http_url(value, name)


def _http_url(value: object, name: str) -> str:
    result = _string(value, name)
    if any(character.isspace() for character in result):
        raise ValueError(f"{name} must not contain whitespace")
    parsed = urlsplit(result)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError(f"{name} must be an http(s) URL")
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(f"{name} cannot carry credentials, paths, queries, or fragments")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{name} has an invalid port") from error
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"{name} has an invalid port")
    return result.rstrip("/")


def _bind_address(value: object, name: str) -> str:
    result = _string(value, name)
    if any(character.isspace() for character in result) or "/" in result:
        raise ValueError(f"{name} must be a host:port bind address")
    port = result[1:] if result.startswith(":") else result.rsplit(":", 1)[-1]
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError(f"{name} must contain a valid port")
    return result


def _bind_port(value: str) -> int:
    return int(value.rsplit(":", 1)[-1])


def _camera_address(value: str) -> None:
    if any(character.isspace() for character in value) or any(mark in value for mark in "?#"):
        raise ValueError("camera_address must be a host without parameters")
    parsed = urlsplit(f"//{value}")
    if parsed.hostname is None or parsed.username is not None or parsed.password is not None:
        raise ValueError("camera_address must not contain credentials")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("camera_address has an invalid port") from error
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("camera_address has an invalid port")
    if parsed.path:
        raise ValueError("camera_address must not contain a path")


def _stream_path(value: str) -> None:
    if (
        not value.startswith("/")
        or any(character.isspace() for character in value)
        or "\x00" in value
        or "?" in value
        or "#" in value
    ):
        raise ValueError("sub_stream_path must be an absolute path without parameters")
    parts = value.split("/")[1:]
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("sub_stream_path must be an absolute path without traversal")


__all__ = [
    "LocalMediaCamera",
    "MediaPathMode",
    "MediaRuntime",
    "MediaRuntimeConfiguration",
    "RecordingMode",
    "ffmpeg_command_for_camera",
    "load_media_runtime_configuration",
    "render_mediamtx_config",
    "validate_sop_camera_bindings",
]
