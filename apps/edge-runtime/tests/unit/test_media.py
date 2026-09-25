"""Issue #34 媒体配置和进程接缝测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from time import time
from uuid import UUID

from edge_runtime.media import (
    LocalMediaCamera,
    MediaPathMode,
    MediaRuntime,
    MediaRuntimeConfiguration,
    RecordingMode,
    load_media_runtime_configuration,
    render_mediamtx_config,
    validate_sop_camera_bindings,
)
from edge_runtime.media_retention import (
    estimate_recording_impact,
    read_applied_window,
    write_applied_window,
)

CAMERA_ID = "018f0000-0000-7000-8000-000000000001"
SECOND_CAMERA_ID = "018f0000-0000-7000-8000-000000000002"
HOST_ID = "018f0000-0000-7000-8000-000000000010"
STATION_ID = "018f0000-0000-7000-8000-000000000020"
MEDIA_PATH = f"camera-{UUID(CAMERA_ID).hex}"
SECOND_MEDIA_PATH = f"camera-{UUID(SECOND_CAMERA_ID).hex}"


class Process:
    def __init__(self, *, exit_code: int | None = None) -> None:
        self.exit_code = exit_code
        self.terminated = False

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True
        self.exit_code = 0

    def kill(self) -> None:
        self.exit_code = -9

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.exit_code = 0 if self.exit_code is None else self.exit_code
        return self.exit_code


class ProcessFactory:
    def __init__(self, outcomes: list[int | None]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[list[str], Process]] = []

    def __call__(self, args: list[str], **_kwargs: object) -> Process:
        process = Process(exit_code=self.outcomes.pop(0) if self.outcomes else None)
        self.calls.append((args, process))
        return process


class MediaFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.username = root / "username"
        self.password = root / "password"
        self.username.write_text("camera-user\n", encoding="utf-8")
        self.password.write_text("camera-secret\n", encoding="utf-8")  # pragma: allowlist secret
        self.username.chmod(0o400)
        self.password.chmod(0o400)
        self.configuration = MediaRuntimeConfiguration(
            host_id=HOST_ID,
            host_status="active",
            mediamtx_address="https://media.example.test:8889",
            mediamtx_playback_address="https://media.example.test:9996",
            recording_window_seconds=3600,
            media_config_path=root / "mediamtx.yml",
            recording_directory=root / "recordings",
            mediamtx_binary=Path("/usr/local/bin/mediamtx"),
            ffmpeg_binary=Path("/usr/bin/ffmpeg"),
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
                self.camera(
                    media_path_mode="passthrough",
                    recording_mode="continuous",
                ),
            ),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def camera(self, *, media_path_mode: str, recording_mode: str) -> LocalMediaCamera:
        return LocalMediaCamera(
            camera_id=CAMERA_ID,
            camera_name="装配相机",
            camera_address="10.0.8.21",
            sub_stream_path="/Streaming/Channels/102",
            camera_status="active",
            station_id=STATION_ID,
            station_status="active",
            host_id=HOST_ID,
            media_path=MEDIA_PATH,
            media_path_mode=MediaPathMode(media_path_mode),
            recording_mode=RecordingMode(recording_mode),
            sop_execution=False,
            credentials_configured=True,
            username_file=self.username,
            password_file=self.password,
        )


class MediaConfigurationTest(MediaFixture):
    def test_render_propagates_control_flow_exit_from_secret_reader(self) -> None:
        def interrupt(path: Path, name: str) -> str:
            del path, name
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            render_mediamtx_config(self.configuration, secret_reader=interrupt)

    def test_parser_preserves_missing_browser_addresses_as_unconfigured(self) -> None:
        configuration = load_media_runtime_configuration(
            {
                "host_id": HOST_ID,
                "host_status": "active",
                "mediamtx_address": None,
                "mediamtx_playback_address": None,
                "recording_window_seconds": 3600,
                "media_config_path": "/etc/nvsop/mediamtx.yml",
                "recording_directory": "/var/lib/nvsop/recordings",
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
            }
        )

        self.assertIsNone(configuration.mediamtx_address)
        self.assertIsNone(configuration.mediamtx_playback_address)

    def test_render_uses_one_stable_path_for_passthrough_and_transcode_modes(self) -> None:
        transcoded = replace(
            self.camera(media_path_mode="cpu_transcode", recording_mode="preview_only"),
            camera_id=SECOND_CAMERA_ID,
            media_path=SECOND_MEDIA_PATH,
        )
        configuration = replace(
            self.configuration, cameras=(self.configuration.cameras[0], transcoded)
        )

        rendered = render_mediamtx_config(configuration)

        self.assertIn("webrtcLocalUDPAddress: ':8189'", rendered)
        self.assertIn(f"{MEDIA_PATH}:", rendered)
        self.assertIn(f"{SECOND_MEDIA_PATH}:", rendered)
        expected_source = (
            "source: 'rtsp://camera-user:"
            "camera-secret@10.0.8.21/Streaming/Channels/102'"  # pragma: allowlist secret
        )
        self.assertIn(expected_source, rendered)
        self.assertIn("source: publisher", rendered)
        self.assertIn("runOnDemand:", rendered)
        self.assertIn("record: yes", rendered)
        self.assertIn("record: no", rendered)
        self.assertNotIn("camera name", rendered)

    def test_deactivated_camera_keeps_a_playback_path_without_source_or_recording(self) -> None:
        camera = replace(self.configuration.cameras[0], camera_status="deactivated")

        rendered = render_mediamtx_config(replace(self.configuration, cameras=(camera,)))

        path_start = rendered.index(f"  {MEDIA_PATH}:")
        path = rendered[path_start:]
        self.assertIn(f"  {MEDIA_PATH}:\n    source: publisher\n    record: no", path)
        self.assertNotIn("camera-user:camera-secret", path)  # pragma: allowlist secret

    def test_removed_center_camera_is_rejected_at_the_media_runtime_boundary(self) -> None:
        with self.assertRaisesRegex(ValueError, "confirmed configuration"):
            validate_sop_camera_bindings(self.configuration, {STATION_ID}, set())

    def test_sop_station_rejects_a_non_sop_camera_at_runtime_boundary(self) -> None:
        with self.assertRaisesRegex(ValueError, "SOP stations"):
            validate_sop_camera_bindings(self.configuration, {STATION_ID}, {CAMERA_ID})

    def test_sop_camera_cannot_use_preview_only_recording(self) -> None:
        camera = replace(
            self.configuration.cameras[0],
            recording_mode=RecordingMode.PREVIEW_ONLY,
            sop_execution=True,
        )
        with self.assertRaisesRegex(ValueError, "SOP cameras"):
            render_mediamtx_config(replace(self.configuration, cameras=(camera,)))

    def test_cpu_continuous_starts_one_ffmpeg_process_per_camera(self) -> None:
        camera = replace(
            self.configuration.cameras[0],
            media_path_mode=MediaPathMode.CPU_TRANSCODE,
        )
        factory = ProcessFactory([None, None])
        runtime = MediaRuntime(
            replace(self.configuration, cameras=(camera,)),
            popen=factory,
        )
        runtime.start()

        # 第一个进程是 MediaMTX, 第二个是该相机唯一的长驻 CPU 路径。
        self.assertEqual("/usr/local/bin/mediamtx", factory.calls[0][0][0])
        self.assertEqual("/usr/bin/ffmpeg", factory.calls[1][0][0])
        runtime.close()

    def test_control_flow_exit_during_ffmpeg_launch_cleans_candidate_resources(self) -> None:
        camera = replace(
            self.configuration.cameras[0],
            media_path_mode=MediaPathMode.CPU_TRANSCODE,
        )
        mediamtx = Process()
        calls = 0

        def popen(args: list[str], **_kwargs: object) -> Process:
            nonlocal calls
            calls += 1
            if calls == 1:
                return mediamtx
            raise KeyboardInterrupt

        runtime = MediaRuntime(replace(self.configuration, cameras=(camera,)), popen=popen)

        with self.assertRaises(KeyboardInterrupt):
            runtime.start()

        self.assertTrue(mediamtx.terminated)
        self.assertFalse(self.configuration.media_config_path.exists())
        self.assertEqual(
            [],
            list(self.configuration.media_config_path.parent.glob(".mediamtx.yml.*.tmp")),
        )

    def test_parser_rejects_a_path_that_is_not_derived_from_the_camera_uuid(self) -> None:
        raw = {
            "host_id": HOST_ID,
            "host_status": "active",
            "mediamtx_address": "https://media.example.test:8889",
            "mediamtx_playback_address": "https://media.example.test:9996",
            "recording_window_seconds": 3600,
            "media_config_path": "/etc/nvsop/mediamtx.yml",
            "recording_directory": "/var/lib/nvsop/recordings",
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
            "cameras": [
                {
                    "camera_id": CAMERA_ID,
                    "camera_name": "相机",
                    "camera_address": "10.0.8.21",
                    "sub_stream_path": "/stream",
                    "camera_status": "active",
                    "station_id": STATION_ID,
                    "station_status": "active",
                    "host_id": HOST_ID,
                    "media_path": "camera-user-name",
                    "media_path_mode": "passthrough",
                    "recording_mode": "continuous",
                    "sop_execution": False,
                    "credentials_configured": False,
                    "credential_files": {"username_file": "/run/u", "password_file": "/run/p"},
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "derived"):
            load_media_runtime_configuration(raw)

    def test_shortening_requires_a_current_real_file_impact_confirmation(self) -> None:
        recording_directory = self.configuration.recording_directory
        recording_directory.mkdir()
        old_segment = recording_directory / "camera" / "old.mp4"
        old_segment.parent.mkdir()
        old_segment.write_bytes(b"old")
        stale_segment = recording_directory / "camera" / "already-expired.mp4"
        stale_segment.write_bytes(b"stale")
        now = time()
        os.utime(old_segment, (now - 2000, now - 2000))
        os.utime(stale_segment, (now - 5000, now - 5000))
        write_applied_window(self.configuration.media_config_path, 3600)
        shorter = replace(self.configuration, recording_window_seconds=1800)
        runtime = MediaRuntime(shorter, popen=ProcessFactory([None]))
        with self.assertRaisesRegex(ValueError, "impact confirmation"):
            runtime.start()

        impact = estimate_recording_impact(
            recording_directory,
            previous_window_seconds=3600,
            target_window_seconds=1800,
            now=now,
            confirmed_by="test-operator",
        )
        confirmed = replace(shorter, recording_window_confirmation=impact)
        # 这里不以布尔开关绕过校验, 候选必须携带明确的影响快照。
        self.assertEqual(3600, read_applied_window(self.configuration.media_config_path))
        self.assertEqual(1, impact.segment_count)
        self.assertEqual(3, impact.bytes)
        runtime.apply(confirmed)
        self.assertEqual(1800, read_applied_window(self.configuration.media_config_path))
        runtime.close()

    def test_reapplying_the_same_configuration_does_not_spawn_a_duplicate_process(self) -> None:
        factory = ProcessFactory([None])
        runtime = MediaRuntime(self.configuration, popen=factory)
        runtime.start()
        runtime.apply(self.configuration)

        self.assertEqual(1, len(factory.calls))
        runtime.close()

    def test_failed_candidate_restores_the_previous_config_file(self) -> None:
        factory = ProcessFactory([None, 1, None])
        runtime = MediaRuntime(self.configuration, popen=factory)
        runtime.start()
        old = self.configuration.media_config_path.read_text(encoding="utf-8")
        candidate = replace(self.configuration, recording_window_seconds=7200)

        with self.assertRaises(RuntimeError):
            runtime.apply(candidate)

        self.assertEqual(old, self.configuration.media_config_path.read_text(encoding="utf-8"))
        self.assertEqual(
            self.configuration.recording_window_seconds,
            runtime.configuration.recording_window_seconds,
        )
        self.assertGreaterEqual(len(factory.calls), 3)


if __name__ == "__main__":
    unittest.main()
