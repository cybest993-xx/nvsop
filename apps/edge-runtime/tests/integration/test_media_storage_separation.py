"""S045 (#208): 滚动录像目录与证据目录必须在写入者启动前就分离。

生产入口 `load_configuration` 是同时看到录像目录、证据目录与 SQLite 路径的唯一 owner; 真实
临时目录 + symlink 别名验证它拒绝重叠、包含与把权威数据库放进滚动录像目录的配置。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from test_station_runtime import _fixture_host_identity

from edge_runtime.configuration import load_configuration

_KEY_FILE = "host-private-key"


def _write_config(
    directory: Path,
    *,
    recording_directory: Path,
    evidence_directory: Path,
    local_state_path: Path,
) -> Path:
    key_file = directory / _KEY_FILE
    key_file.write_text(_fixture_host_identity(11).private_key + "\n", encoding="utf-8")
    config = {
        "center_url": "https://center.example",
        "host_id": "host-for-test",
        "host_private_key_file": str(key_file),
        "command_timeout_seconds": 2.0,
        "command_poll_interval_seconds": 1.0,
        "connectors": [],
        "local_state_path": str(local_state_path),
        "stations": [
            {
                "station_id": "station-1",
                "inference_url": "http://inference.example/v1/chat/completions",
                "request": {"messages": [], "stream": True},
                "template": {
                    "steps": ["(1) start"],
                    "ordering": "ordered",
                    "start_signal": "(1) start",
                    "end_signals": [],
                },
                "parameters": {"idle_timeout": 30, "step_deadline": 10},
                "margins": {"leading": 1, "trailing": 1},
            }
        ],
        "media": {
            "host_id": "host-for-test",
            "host_status": "active",
            "mediamtx_address": None,
            "mediamtx_playback_address": None,
            "recording_window_seconds": 3600,
            "media_config_path": str(directory / "mediamtx.yml"),
            "recording_directory": str(recording_directory),
            "mediamtx_binary": "/usr/local/bin/mediamtx",
            "ffmpeg_binary": "/usr/bin/ffmpeg",
            "rtsp_bind_address": ":8554",
            "webrtc_bind_address": ":8889",
            "webrtc_udp_bind_address": ":8189",
            "playback_bind_address": ":9996",
            "allow_origins": ["https://control.example.test"],
            "record_segment_duration_seconds": 60,
            "preview_release_delay_seconds": 5,
            "startup_timeout_seconds": 2,
            "transcode_threads": 2,
            "cameras": [],
        },
        "evidence": {
            "evidence_directory": str(evidence_directory),
            "ffprobe_binary": "/usr/bin/ffprobe",
            "slice_timeout_seconds": 30,
        },
    }
    config_file = directory / "config.json"
    config_file.write_text(json.dumps(config), encoding="utf-8")
    return config_file


class MediaStorageSeparationTests(unittest.TestCase):
    def test_production_config_rejects_recording_and_evidence_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recordings = root / "recordings"
            evidence = root / "evidence"
            recordings.mkdir()
            evidence.mkdir()

            separate = _write_config(
                root,
                recording_directory=recordings,
                evidence_directory=evidence,
                local_state_path=root / "state.sqlite",
            )
            # 分离配置是基线, 不应被安全校验误伤。
            self.assertIsNotNone(load_configuration(separate, include_stations=True))

    def test_production_config_rejects_nested_or_aliased_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recordings = root / "recordings"
            evidence = root / "evidence"
            shared = root / "shared"
            recordings.mkdir()
            evidence.mkdir()
            shared.mkdir()
            evidence_alias = root / "evidence-alias"
            os.symlink(shared, evidence_alias)
            recording_alias = root / "recording-alias"
            os.symlink(shared, recording_alias)

            cases = {
                "same-directory": (recordings, recordings, root / "state.sqlite"),
                "evidence-inside-recording": (
                    recordings,
                    recordings / "clips",
                    root / "state.sqlite",
                ),
                "recording-inside-evidence": (
                    evidence / "raw",
                    evidence,
                    root / "state.sqlite",
                ),
                "evidence-symlink-alias": (shared, evidence_alias, root / "state.sqlite"),
                "recording-symlink-alias": (recording_alias, shared, root / "state.sqlite"),
                "state-database-inside-recording": (
                    recordings,
                    evidence,
                    recordings / "state.sqlite",
                ),
            }
            for name, (recording, evidence_directory, state_path) in cases.items():
                with self.subTest(case=name):
                    config_file = _write_config(
                        root,
                        recording_directory=recording,
                        evidence_directory=evidence_directory,
                        local_state_path=state_path,
                    )
                    with self.assertRaises(ValueError):
                        load_configuration(config_file, include_stations=True)


if __name__ == "__main__":
    unittest.main()
