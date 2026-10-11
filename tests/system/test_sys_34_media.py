"""Issue #34 的真实 MediaMTX 证据；此处不接受伪造服务。"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

import pytest

_PLAYBACK_LIST_URL = os.environ.get("NVSOP_MEDIA_PLAYBACK_LIST_URL")
_PLAYBACK_GET_URL = os.environ.get("NVSOP_MEDIA_PLAYBACK_GET_URL")
_CPU_PLAYBACK_LIST_URL = os.environ.get("NVSOP_MEDIA_CPU_PLAYBACK_LIST_URL")
_CPU_PLAYBACK_GET_URL = os.environ.get("NVSOP_MEDIA_CPU_PLAYBACK_GET_URL")
_EXPECTED_PATH = os.environ.get("NVSOP_MEDIA_EXPECTED_PATH", "")
_CPU_EXPECTED_PATH = os.environ.get("NVSOP_MEDIA_CPU_EXPECTED_PATH", "cpu-transcoded")
# 真实协议夹具入口：按需路径 reader/source 断言与源/容器控制需要这些变量。
_PLAYBACK_URL = os.environ.get("NVSOP_MEDIA_PLAYBACK_URL")
_API_URL = os.environ.get("NVSOP_MEDIA_API_URL")
_RTSP_URL = os.environ.get("NVSOP_MEDIA_RTSP_URL")
_COMPOSE_PROJECT = os.environ.get("NVSOP_MEDIA_COMPOSE_PROJECT")
_COMPOSE_FILE = os.environ.get("NVSOP_MEDIA_COMPOSE_FILE")
_FFMPEG = os.environ.get("NVSOP_FFMPEG", "ffmpeg")

_LIVE_PROTOCOL_FIXTURE = all(
    value is not None
    for value in (_PLAYBACK_URL, _API_URL, _RTSP_URL, _COMPOSE_PROJECT, _COMPOSE_FILE)
)

requires_protocol_fixture = pytest.mark.skipif(
    not _LIVE_PROTOCOL_FIXTURE,
    reason="requires the live MediaMTX management API and Compose fixture (make media-system)",
)


def _assert_playable_video(url: str) -> None:
    """真实解码整段：ffprobe -count_frames 必须至少解出一帧视频。"""
    request = Request(url, headers={"Accept": "video/mp4"})
    with urlopen(request, timeout=20) as response:
        assert response.headers.get_content_type() == "video/mp4"
        with tempfile.NamedTemporaryFile(suffix=".mp4") as sample_file:
            sample_file.write(response.read())
            sample_file.flush()
            probe = subprocess.run(
                [
                    os.environ.get("NVSOP_FFPROBE", "ffprobe"),
                    "-v",
                    "error",
                    "-count_frames",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=codec_type,nb_read_frames",
                    "-of",
                    "json",
                    sample_file.name,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
    streams = json.loads(probe.stdout).get("streams", [])
    assert streams, "playback /get returned no decodable video stream"
    assert any(
        stream.get("codec_type") == "video" and int(stream.get("nb_read_frames") or 0) > 0
        for stream in streams
    ), "playback /get did not decode any video frame"


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _get_json(url: str, timeout: float = 10) -> object:
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _path_state(path: str) -> dict[str, Any]:
    payload = _get_json(f"{_API_URL}/v3/paths/get/{path}")
    assert isinstance(payload, dict)
    return payload


def _path_ready(path: str) -> bool:
    return bool(_path_state(path)["ready"])


def _path_readers(path: str) -> int:
    return len(_path_state(path)["readers"])


def _readers_match(path: str, baseline: int) -> bool:
    return _path_readers(path) == baseline


def _wait_until(predicate: Callable[[], bool], timeout: float, description: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {description}")


def _list_intervals(path: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    query = urlencode({"path": path, "start": _iso(start), "end": _iso(end)})
    try:
        payload = _get_json(f"{_PLAYBACK_URL}/list?{query}")
    except HTTPError as error:
        # MediaMTX 在没有覆盖该窗口的分段时返回 404；此处视为“无录像”。
        if error.code == 404:
            return []
        raise
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict) and item.get("start")]


def _recording_end(path: str) -> datetime | None:
    now = datetime.now(UTC)
    intervals = _list_intervals(path, now - timedelta(minutes=10), now + timedelta(seconds=30))
    ends = [
        datetime.fromisoformat(str(item["start"]).replace("Z", "+00:00"))
        + timedelta(seconds=float(item["duration"]))
        for item in intervals
    ]
    return max(ends) if ends else None


def _recording_after(path: str, moment: datetime) -> bool:
    """窗口起点晚于 moment，避免用合并 /list 区间的最初 start 冒充新分段。"""
    return bool(_list_intervals(path, moment, datetime.now(UTC) + timedelta(seconds=30)))


def _playback_get_url(path: str, interval: dict[str, Any]) -> str:
    query = urlencode(
        {
            "path": path,
            "start": interval["start"],
            "duration": interval["duration"],
            "format": "mp4",
        }
    )
    return f"{_PLAYBACK_URL}/get?{query}"


def _compose() -> list[str]:
    return ["docker", "compose", "-p", str(_COMPOSE_PROJECT), "-f", str(_COMPOSE_FILE)]


def _compose_command(*arguments: str) -> None:
    subprocess.run([*_compose(), *arguments], check=True, capture_output=True, text=True)


def _mediamtx_has_ffmpeg() -> bool:
    completed = subprocess.run(
        [*_compose(), "exec", "-T", "mediamtx", "ps", "-o", "args"],
        check=True,
        capture_output=True,
        text=True,
    )
    return any("ffmpeg" in line for line in completed.stdout.splitlines())


def _api_ready() -> bool:
    try:
        _path_state("synthetic")
    except (OSError, HTTPError, URLError, json.JSONDecodeError, KeyError):
        return False
    return True


class _RtspReader:
    """真实 RTSP reader；生命周期由 _rtsp_reader 管理，不自行关闭日志文件。"""

    def __init__(self, path: str, log: BinaryIO) -> None:
        self._log = log
        self._process = subprocess.Popen(
            [
                _FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-rtsp_transport",
                "tcp",
                "-i",
                f"{_RTSP_URL}/{path}",
                "-f",
                "null",
                "-",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log,
        )

    def exited(self) -> bool:
        return self._process.poll() is not None

    def diagnostics(self) -> str:
        self._log.seek(0)
        return self._log.read().decode("utf-8", errors="replace")[-2000:]

    def stop(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
        try:
            self._process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=15)


@contextlib.contextmanager
def _rtsp_reader(path: str) -> Iterator[_RtspReader]:
    """启动真实 RTSP reader，离开 with 块时释放，避免污染 path 的 reader 计数。"""
    with tempfile.TemporaryFile() as log:
        reader = _RtspReader(path, log)
        try:
            yield reader
        finally:
            reader.stop()


def _wait_for_reader(path: str, reader: _RtspReader, minimum: int = 1, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if reader.exited():
            raise AssertionError(f"RTSP reader for {path} exited early: {reader.diagnostics()}")
        if _path_readers(path) >= minimum:
            return
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for a reader on {path}: {reader.diagnostics()}")


@pytest.mark.skipif(
    _PLAYBACK_LIST_URL is None,
    reason="requires a live MediaMTX playback /list URL",
)
def test_live_mediamtx_returns_intervals_for_the_expected_stable_path() -> None:
    assert _PLAYBACK_LIST_URL is not None
    request = Request(_PLAYBACK_LIST_URL, headers={"Accept": "application/json"})
    with urlopen(request, timeout=10) as response:
        payload = json.load(response)
    assert isinstance(payload, list)
    assert payload
    assert all(
        isinstance(item, dict)
        and isinstance(item.get("start"), str)
        and isinstance(item.get("duration"), (int, float))
        and item["duration"] >= 0
        for item in payload
    )
    if _EXPECTED_PATH:
        assert parse_qs(urlsplit(_PLAYBACK_LIST_URL).query).get("path") == [_EXPECTED_PATH]


@pytest.mark.skipif(
    _PLAYBACK_GET_URL is None,
    reason="requires a live MediaMTX playback /get URL",
)
def test_live_mediamtx_get_returns_playable_video() -> None:
    assert _PLAYBACK_GET_URL is not None
    _assert_playable_video(_PLAYBACK_GET_URL)


@pytest.mark.skipif(
    _CPU_PLAYBACK_LIST_URL is None,
    reason="requires a live CPU-transcoded MediaMTX playback /list URL",
)
def test_live_mediamtx_cpu_transcode_returns_intervals_for_the_expected_path() -> None:
    assert _CPU_PLAYBACK_LIST_URL is not None
    request = Request(_CPU_PLAYBACK_LIST_URL, headers={"Accept": "application/json"})
    with urlopen(request, timeout=10) as response:
        payload = json.load(response)
    assert isinstance(payload, list)
    assert payload
    assert all(
        isinstance(item, dict)
        and isinstance(item.get("start"), str)
        and isinstance(item.get("duration"), (int, float))
        and item["duration"] >= 0
        for item in payload
    )
    assert parse_qs(urlsplit(_CPU_PLAYBACK_LIST_URL).query).get("path") == [_CPU_EXPECTED_PATH]


@pytest.mark.skipif(
    _CPU_PLAYBACK_GET_URL is None,
    reason="requires a live CPU-transcoded MediaMTX playback /get URL",
)
def test_live_mediamtx_cpu_transcode_get_returns_playable_video() -> None:
    assert _CPU_PLAYBACK_GET_URL is not None
    _assert_playable_video(_CPU_PLAYBACK_GET_URL)


@requires_protocol_fixture
def test_on_demand_passthrough_starts_for_a_reader_and_releases_after_it_leaves() -> None:
    """SYS-34-04：passthrough 按需路径只在真实 reader 存在时 ready，最后一位退出并到期后释放。"""
    path = "on-demand-passthrough"
    _wait_until(lambda: not _path_ready(path), 20, f"{path} idle with no reader")
    assert _path_readers(path) == 0

    with _rtsp_reader(path) as reader:
        _wait_for_reader(path, reader)
        assert _path_ready(path)

    _wait_until(lambda: not _path_ready(path), 20, f"{path} released after the last reader")
    assert _path_readers(path) == 0


@requires_protocol_fixture
def test_on_demand_cpu_transcode_releases_without_leftover_ffmpeg() -> None:
    """SYS-34-04：CPU 转码按需路径在最后 reader 退出后释放容器内 ffmpeg。"""
    path = "on-demand-cpu"
    _wait_until(lambda: not _path_ready(path), 20, f"{path} idle with no reader")
    assert _path_readers(path) == 0
    assert not _mediamtx_has_ffmpeg()

    with _rtsp_reader(path) as reader:
        _wait_for_reader(path, reader)
        assert _path_ready(path)
        _wait_until(_mediamtx_has_ffmpeg, 20, "runOnDemand ffmpeg inside mediamtx")

    _wait_until(lambda: not _path_ready(path), 20, f"{path} released after the last reader")
    assert _path_readers(path) == 0
    _wait_until(lambda: not _mediamtx_has_ffmpeg(), 20, "no leftover ffmpeg in mediamtx")


@requires_protocol_fixture
def test_continuous_recording_keeps_segments_before_and_after_viewers() -> None:
    """SYS-34-05：两路连续录像在首位 viewer 之前与全部 viewer 退出后都产生新分段。"""
    for path in ("synthetic", "cpu-transcoded"):
        # synthetic 自身有 cpu-transcoder 的 upstream reader，退出后只要求回到该基线。
        baseline_readers = _path_readers(path)
        initial_end = _recording_end(path)
        assert initial_end is not None, f"{path} recorded nothing before any viewer"

        with _rtsp_reader(path) as reader:
            # 等真实 viewer 加入，而不是被 synthetic 自有的 upstream reader 直接满足。
            _wait_for_reader(path, reader, minimum=baseline_readers + 1)
            time.sleep(3)
        closed_at = datetime.now(UTC)

        _wait_until(
            partial(_readers_match, path, baseline_readers),
            20,
            f"{path} readers returned to the pre-viewer baseline",
        )
        _wait_until(
            partial(_recording_after, path, closed_at + timedelta(seconds=1)),
            30,
            f"{path} produced a later segment after the last viewer left",
        )


@requires_protocol_fixture
def test_synthetic_source_disconnect_marks_path_not_ready_and_restore_recovers() -> None:
    """SYS-34-08/09 协议层：源断开使 path not-ready，恢复后 ready 并产生可解码新分段。"""
    path = "synthetic"
    _wait_until(lambda: _path_ready(path), 20, f"{path} ready before source disconnect")

    _compose_command("stop", "synthetic-rtsp")
    _wait_until(lambda: not _path_ready(path), 30, f"{path} not ready after source disconnect")

    _compose_command("start", "synthetic-rtsp")
    _wait_until(lambda: _path_ready(path), 30, f"{path} ready after source restore")
    restored_at = datetime.now(UTC)
    _wait_until(
        lambda: _recording_after(path, restored_at),
        30,
        f"{path} produced a new segment after source restore",
    )

    # 只取恢复时刻之后的窗口，避免合并区间把断开前的旧录像也算成恢复证据。
    restored = _list_intervals(path, restored_at, datetime.now(UTC) + timedelta(seconds=30))
    assert restored, "no new segment after source restore"
    _assert_playable_video(_playback_get_url(path, restored[0]))


@requires_protocol_fixture
def test_history_stays_playable_after_mediamtx_restart() -> None:
    """SYS-34-11：夹具不依赖中心启动；重启 mediamtx 后历史区间仍可 /list 与 /get 解码。"""
    path = "synthetic"
    window_end = datetime.now(UTC) - timedelta(seconds=5)
    window_start = window_end - timedelta(minutes=3)
    historical = _list_intervals(path, window_start, window_end)
    assert historical, "no historical intervals available before restart"
    interval = historical[0]

    _compose_command("restart", "mediamtx")
    _wait_until(_api_ready, 60, "management API after mediamtx restart")
    _wait_until(
        lambda: bool(_list_intervals(path, window_start, window_end)),
        60,
        "historical intervals after mediamtx restart",
    )
    _assert_playable_video(_playback_get_url(path, interval))
