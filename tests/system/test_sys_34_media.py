"""Issue #34 的真实 MediaMTX 证据；此处不接受伪造服务。"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
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
_ADMIN_URL = os.environ.get("NVSOP_MEDIA_ADMIN_URL")
_RTSP_URL = os.environ.get("NVSOP_MEDIA_RTSP_URL")
_COMPOSE_PROJECT = os.environ.get("NVSOP_MEDIA_COMPOSE_PROJECT")
_COMPOSE_FILE = os.environ.get("NVSOP_MEDIA_COMPOSE_FILE")
_FIXTURE_READY = all([_ADMIN_URL, _RTSP_URL, _COMPOSE_PROJECT, _COMPOSE_FILE])


def _assert_playable_video(url: str) -> None:
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
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "json",
                    sample_file.name,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
    streams = json.loads(probe.stdout).get("streams", [])
    assert any(stream.get("codec_type") == "video" for stream in streams)


def _admin_path(name: str) -> dict[str, Any]:
    request = Request(f"{_ADMIN_URL}/v3/paths/get/{name}", headers={"Accept": "application/json"})
    with urlopen(request, timeout=5) as response:
        payload = json.load(response)
    assert isinstance(payload, dict)
    return payload


def _wait_for_path(
    name: str, predicate: Callable[[dict[str, Any]], bool], timeout: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = _admin_path(name)
        if predicate(last):
            return last
        time.sleep(0.5)
    raise AssertionError(f"MediaMTX path {name} did not reach the expected state: {last}")


def _wait_for_admin(timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{_ADMIN_URL}/v3/paths/list", timeout=2):
                return
        except (HTTPError, URLError, OSError):
            time.sleep(0.5)
    raise AssertionError("MediaMTX admin API did not come back after restart")


def _compose(*arguments: str) -> None:
    completed = subprocess.run(
        ["docker", "compose", "-p", str(_COMPOSE_PROJECT), "-f", str(_COMPOSE_FILE), *arguments],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    if completed.returncode != 0:
        raise AssertionError(f"docker compose {arguments} failed: {completed.stderr.strip()}")


def _mediamtx_container() -> str:
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            str(_COMPOSE_PROJECT),
            "-f",
            str(_COMPOSE_FILE),
            "ps",
            "-q",
            "mediamtx",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    container = completed.stdout.strip()
    assert container, "mediamtx container must be running"
    return container


@contextlib.contextmanager
def _rtsp_reader(path: str) -> Iterator[subprocess.Popen[bytes]]:
    process = subprocess.Popen(
        [
            "ffmpeg",
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
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)


def _playback_base() -> str:
    parts = urlsplit(str(_PLAYBACK_LIST_URL))
    return f"{parts.scheme}://{parts.netloc}"


def _list_intervals(path: str, start: str, end: str) -> list[dict[str, Any]]:
    url = f"{_playback_base()}/list?{urlencode({'path': path, 'start': start, 'end': end})}"
    with urlopen(url, timeout=10) as response:
        payload = json.load(response)
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict) and item.get("start")]


def _get_url(path: str, interval: dict[str, Any]) -> str:
    query = {
        "path": path,
        "start": interval["start"],
        "duration": interval["duration"],
        "format": "mp4",
    }
    return f"{_playback_base()}/get?{urlencode(query)}"


def _utc_window() -> tuple[str, str]:
    now = datetime.now(UTC)
    return (
        (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
    )


def _recording_end(intervals: list[dict[str, Any]]) -> datetime:
    return max(
        datetime.fromisoformat(str(item["start"]).replace("Z", "+00:00"))
        + timedelta(seconds=float(item["duration"]))
        for item in intervals
    )


def _wait_for_newer_interval(
    path: str, start: str, end: str, after: str, timeout: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        candidates = [item for item in _list_intervals(path, start, end) if item["start"] > after]
        if candidates:
            return min(candidates, key=lambda item: item["start"])
        time.sleep(1)
    raise AssertionError(f"no new segment appeared for {path} after {after}")


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


@pytest.mark.skipif(not _FIXTURE_READY, reason="requires the live MediaMTX admin/RTSP fixture")
def test_sys_34_04_on_demand_pulls_source_only_for_connected_readers() -> None:
    before = _admin_path("on-demand")
    assert before["ready"] is False
    assert before["readers"] == []

    with _rtsp_reader("on-demand"):
        during = _wait_for_path(
            "on-demand", lambda path: bool(path["ready"]) and len(path["readers"]) >= 1, 20
        )
        assert during["tracks"], "on-demand source must expose a decodable track"

    released = _wait_for_path(
        "on-demand", lambda path: not path["ready"] and not path["readers"], 20
    )
    assert released["readers"] == []


@pytest.mark.skipif(not _FIXTURE_READY, reason="requires the live MediaMTX admin/RTSP fixture")
def test_sys_34_04_cpu_transcode_on_demand_releases_without_leftover_process() -> None:
    before = _admin_path("transcode-on-demand")
    assert before["ready"] is False
    assert before["readers"] == []
    container = _mediamtx_container()

    with _rtsp_reader("transcode-on-demand"):
        during = _wait_for_path(
            "transcode-on-demand",
            lambda path: bool(path["ready"]) and len(path["readers"]) >= 1,
            25,
        )
        assert during["tracks"], "CPU transcode path must expose a decodable track"
        running = subprocess.run(
            ["docker", "exec", container, "pgrep", "-f", "transcode-on-demand"],
            capture_output=True,
            text=True,
        )
        assert running.returncode == 0, "runOnDemand ffmpeg must run while a reader is connected"

    _wait_for_path(
        "transcode-on-demand", lambda path: not path["ready"] and not path["readers"], 25
    )
    leftover = subprocess.run(
        ["docker", "exec", container, "pgrep", "-f", "transcode-on-demand"],
        capture_output=True,
        text=True,
    )
    assert leftover.returncode != 0, f"runOnDemand ffmpeg must exit: {leftover.stdout}"


@pytest.mark.skipif(
    _PLAYBACK_LIST_URL is None or not _FIXTURE_READY,
    reason="requires the live MediaMTX playback/admin/RTSP fixture",
)
def test_sys_34_05_continuous_recording_before_and_after_viewers() -> None:
    start, end = _utc_window()
    before = _list_intervals("synthetic", start, end)
    assert before, "continuous recording must produce segments before the first viewer"
    end_before = _recording_end(before)

    with _rtsp_reader("synthetic"):
        _wait_for_path("synthetic", lambda path: len(path["readers"]) >= 1, 15)

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        after = _list_intervals("synthetic", start, end)
        if after and _recording_end(after) > end_before:
            return
        time.sleep(1)
    raise AssertionError("continuous recording must keep growing after viewers exit")


@pytest.mark.skipif(
    _PLAYBACK_LIST_URL is None or not _FIXTURE_READY,
    reason="requires the live MediaMTX playback/admin/RTSP fixture",
)
def test_sys_34_08_09_source_disconnect_recovers_recording_and_playback() -> None:
    start, end = _utc_window()
    before = _list_intervals("synthetic", start, end)
    assert before
    latest_before = max(item["start"] for item in before)

    _compose("stop", "synthetic-rtsp")
    _wait_for_path("synthetic", lambda path: not path["ready"], 20)

    _compose("start", "synthetic-rtsp")
    _wait_for_path("synthetic", lambda path: bool(path["ready"]), 30)

    recovered = _wait_for_newer_interval("synthetic", start, end, latest_before, 40)
    _assert_playable_video(_get_url("synthetic", recovered))


@pytest.mark.skipif(
    _PLAYBACK_LIST_URL is None or not _FIXTURE_READY,
    reason="requires the live MediaMTX playback/admin/RTSP fixture",
)
def test_sys_34_11_restart_keeps_history_playable_without_center() -> None:
    start, end = _utc_window()
    before = _list_intervals("synthetic", start, end)
    assert before
    historical = min(before, key=lambda item: item["start"])

    _compose("restart", "mediamtx")
    _wait_for_admin(30)

    after = _list_intervals("synthetic", start, end)
    assert any(item["start"] == historical["start"] for item in after), (
        "MediaMTX restart must keep historical segments listed"
    )
    _assert_playable_video(_get_url("synthetic", historical))
