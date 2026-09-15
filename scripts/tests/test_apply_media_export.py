"""验证中心导出合并到本机策略时不携带 secret 内容。"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from time import time

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "nvsop_apply_media_export", ROOT / "scripts" / "apply_media_export.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ApplyMediaExportTest(unittest.TestCase):
    def test_merge_keeps_local_runtime_settings_and_only_selected_camera_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            export = root / "export.json"
            edge = root / "edge.json"
            output = root / "out.json"
            host_id = "018f0000-0000-7000-8000-000000000010"
            camera_id = "018f0000-0000-7000-8000-000000000001"
            export.write_text(
                json.dumps(
                    {
                        "host_id": host_id,
                        "host_name": "推理机",
                        "host_revision": 1,
                        "host_status": "active",
                        "mediamtx_address": "https://media.example.test:8889",
                        "mediamtx_playback_address": "https://media.example.test:9996",
                        "recording_window_seconds": 3600,
                        "cameras": [
                            {
                                "camera_id": camera_id,
                                "camera_name": "相机",
                                "camera_address": "10.0.8.21",
                                "main_stream_path": "/main",
                                "sub_stream_path": "/stream",
                                "camera_status": "active",
                                "camera_revision": 1,
                                "station_id": "018f0000-0000-7000-8000-000000000020",
                                "station_name": "工位",
                                "station_status": "active",
                                "host_id": host_id,
                                "host_name": "推理机",
                                "host_status": "active",
                                "backend_id": "018f0000-0000-7000-8000-000000000030",
                                "media_path": f"camera-{camera_id.replace('-', '')}",
                                "media_path_mode": "passthrough",
                                "recording_mode": "continuous",
                                "credentials_configured": True,
                                "mediamtx_address": "https://media.example.test:8889",
                                "mediamtx_playback_address": "https://media.example.test:9996",
                                "recording_window_seconds": 3600,
                                "credential_files": {
                                    "username_file": "/run/secrets/user",
                                    "password_file": "/run/secrets/password",
                                },
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            edge.write_text(
                json.dumps(
                    {
                        "media": {
                            "media_config_path": str(root / "mediamtx.yml"),
                            "recording_directory": str(root / "recordings"),
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
                            "cameras": [{"camera_id": camera_id, "sop_execution": False}],
                        }
                    }
                ),
                encoding="utf-8",
            )

            MODULE.apply_export(export, edge, output)

            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(str(root / "recordings"), result["media"]["recording_directory"])
            self.assertEqual(camera_id, result["media"]["cameras"][0]["camera_id"])
            self.assertFalse(result["media"]["cameras"][0]["sop_execution"])
            rendered = json.dumps(result)
            self.assertNotIn("camera-secret", rendered)
            self.assertNotIn("edge-user-for-test", rendered)
            self.assertNotIn("edge-password-for-test", rendered)
            self.assertEqual(0o600, output.stat().st_mode & 0o777)

            recordings = root / "recordings" / "camera"
            recordings.mkdir(parents=True)
            segment = recordings / "segment.mp4"
            segment.write_bytes(b"segment")
            now = time()
            os.utime(segment, (now - 2000, now - 2000))
            (root / "mediamtx.yml.applied").write_text(
                '{"recording_window_seconds":3600}\n', encoding="utf-8"
            )
            shrinking_export = json.loads(export.read_text(encoding="utf-8"))
            shrinking_export["recording_window_seconds"] = 1800
            shrinking = root / "shrinking.json"
            shrinking.write_text(json.dumps(shrinking_export), encoding="utf-8")
            confirmed = root / "confirmed.json"

            impact = MODULE.apply_export(
                shrinking,
                output,
                confirmed,
                confirm_retention=True,
                operator="operator-1",
            )

            self.assertIsNotNone(impact)
            assert impact is not None
            self.assertEqual(1, impact["segment_count"])
            self.assertEqual("operator-1", impact["confirmed_by"])
            confirmed_media = json.loads(confirmed.read_text(encoding="utf-8"))["media"]
            self.assertEqual(1800, confirmed_media["recording_window_seconds"])
            self.assertEqual(
                "operator-1", confirmed_media["recording_window_confirmation"]["confirmed_by"]
            )


if __name__ == "__main__":
    unittest.main()
