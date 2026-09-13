#!/usr/bin/env python3
"""将中心媒体导出合并到推理机配置，绝不复制 secret 内容。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

_STATIC_MEDIA_KEYS = frozenset(
    {
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
    }
)
_EXPORT_KEYS = frozenset(
    {
        "host_id",
        "host_name",
        "host_revision",
        "host_status",
        "mediamtx_address",
        "mediamtx_playback_address",
        "recording_window_seconds",
        "cameras",
    }
)
_EXPORT_CAMERA_KEYS = frozenset(
    {
        "camera_id",
        "camera_name",
        "camera_address",
        "main_stream_path",
        "sub_stream_path",
        "camera_status",
        "camera_revision",
        "station_id",
        "station_name",
        "station_status",
        "host_id",
        "host_name",
        "host_status",
        "backend_id",
        "media_path",
        "media_path_mode",
        "recording_mode",
        "credentials_configured",
        "mediamtx_address",
        "mediamtx_playback_address",
        "recording_window_seconds",
        "credential_files",
    }
)
_LOCAL_CAMERA_KEYS = frozenset(
    {
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
    }
)


def apply_export(
    export_path: Path,
    edge_path: Path,
    output_path: Path,
    *,
    confirm_retention: bool = False,
    operator: str | None = None,
) -> dict[str, object] | None:
    export = _object(json.loads(export_path.read_text(encoding="utf-8")), "center export")
    _require_keys(export, _EXPORT_KEYS, "center export")
    edge = _object(json.loads(edge_path.read_text(encoding="utf-8")), "edge configuration")
    local_media = _object(edge.get("media"), "edge media configuration")
    missing = _STATIC_MEDIA_KEYS - local_media.keys()
    if missing:
        raise ValueError(f"edge media configuration is missing: {', '.join(sorted(missing))}")

    host_id = _string(export.get("host_id"), "host_id")
    existing_cameras = {
        _string(camera.get("camera_id"), "local camera_id"): camera
        for camera in _array(local_media.get("cameras", []), "local cameras")
        if isinstance(camera, dict) and isinstance(camera.get("camera_id"), str)
    }
    cameras = [
        _camera(item, host_id, existing_cameras.get(_string(item.get("camera_id"), "camera_id")))
        for item in _array(export.get("cameras"), "cameras")
    ]
    target_window = export.get("recording_window_seconds")
    media = {
        **{key: local_media[key] for key in _STATIC_MEDIA_KEYS},
        "host_id": host_id,
        "host_status": _string(export.get("host_status"), "host_status"),
        "mediamtx_address": _optional_string(export.get("mediamtx_address"), "mediamtx_address"),
        "mediamtx_playback_address": _optional_string(
            export.get("mediamtx_playback_address"), "mediamtx_playback_address"
        ),
        "recording_window_seconds": target_window,
        "cameras": cameras,
    }
    if (
        local_media.get("recording_window_seconds") == target_window
        and "recording_window_confirmation" in local_media
    ):
        media["recording_window_confirmation"] = local_media["recording_window_confirmation"]
    _validate_media(media)
    impact: dict[str, object] | None = None
    if confirm_retention:
        if operator is None or not operator.strip():
            raise ValueError("--operator is required with --confirm-retention")
        configuration = _load_media(media)
        previous_window = _read_applied_window(configuration.media_config_path)
        if previous_window is not None and configuration.recording_window_seconds < previous_window:
            measured = _estimate_impact(
                configuration.recording_directory,
                previous_window_seconds=previous_window,
                target_window_seconds=configuration.recording_window_seconds,
                confirmed_by=operator.strip(),
            )
            impact = measured.to_wire()
            media["recording_window_confirmation"] = impact
            _validate_media(media)
    edge["media"] = media
    _write_atomic(output_path, edge)
    return impact


def _camera(value: object, host_id: str, existing: dict[str, object] | None) -> dict[str, object]:
    source = _object(value, "center export camera")
    _require_keys(source, _EXPORT_CAMERA_KEYS, "center export camera")
    if source["host_id"] != host_id:
        raise ValueError("center export camera belongs to another host")
    try:
        camera_id = UUID(_string(source["camera_id"], "camera_id"))
    except ValueError as error:
        raise ValueError("camera_id must be a UUID") from error
    if source["media_path"] != f"camera-{camera_id.hex}":
        raise ValueError("media_path must be derived from camera_id")
    selected = {key: source[key] for key in _LOCAL_CAMERA_KEYS if key != "sop_execution"}
    selected["sop_execution"] = (
        _boolean(existing["sop_execution"], "sop_execution")
        if existing is not None and "sop_execution" in existing
        else True
    )
    return selected


class _RecordingImpact(Protocol):
    def to_wire(self) -> dict[str, object]: ...


class _MediaConfiguration(Protocol):
    media_config_path: Path
    recording_directory: Path
    recording_window_seconds: int


def _edge_media_functions() -> tuple[
    Callable[[object], _MediaConfiguration],
    Callable[..., _RecordingImpact],
    Callable[[Path], int | None],
]:
    try:
        from edge_runtime.media import load_media_runtime_configuration
        from edge_runtime.media_retention import estimate_recording_impact, read_applied_window
    except ModuleNotFoundError:
        source = Path(__file__).resolve().parents[1] / "apps" / "edge-runtime" / "src"
        if str(source) not in sys.path:
            sys.path.insert(0, str(source))
        from edge_runtime.media import load_media_runtime_configuration
        from edge_runtime.media_retention import estimate_recording_impact, read_applied_window
    return load_media_runtime_configuration, estimate_recording_impact, read_applied_window


def _load_media(value: dict[str, object]) -> _MediaConfiguration:
    load_media_runtime_configuration, _, _ = _edge_media_functions()
    return load_media_runtime_configuration(value)


def _estimate_impact(
    directory: Path,
    *,
    previous_window_seconds: int,
    target_window_seconds: int,
    confirmed_by: str,
) -> _RecordingImpact:
    _, estimate_recording_impact, _ = _edge_media_functions()
    return estimate_recording_impact(
        directory,
        previous_window_seconds=previous_window_seconds,
        target_window_seconds=target_window_seconds,
        confirmed_by=confirmed_by,
    )


def _read_applied_window(path: Path) -> int | None:
    _, _, read_applied_window = _edge_media_functions()
    return read_applied_window(path)


def _validate_media(value: dict[str, object]) -> None:
    _load_media(value)


def _require_keys(value: dict[str, Any], required: frozenset[str], name: str) -> None:
    missing = required - value.keys()
    unknown = value.keys() - required
    if missing:
        raise ValueError(f"{name} is missing: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{name} has unsupported fields: {', '.join(sorted(unknown))}")


def _write_atomic(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _array(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_string(value: object, name: str) -> str | None:
    return None if value is None else _string(value, name)


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path, help="center export JSON")
    parser.add_argument("edge_config", type=Path, help="local edge JSON containing media policy")
    parser.add_argument("output", type=Path, help="new edge JSON")
    parser.add_argument(
        "--confirm-retention",
        action="store_true",
        help="确认并记录基于本机录像目录的窗口缩短影响",
    )
    parser.add_argument("--operator", help="执行录像窗口确认的本机操作员标识")
    args = parser.parse_args()
    impact = apply_export(
        args.export,
        args.edge_config,
        args.output,
        confirm_retention=args.confirm_retention,
        operator=args.operator,
    )
    if impact is not None:
        sys.stdout.write(
            "recording-window impact confirmed: "
            f"{impact['segment_count']} segments, {impact['bytes']} bytes, "
            f"{impact['oldest_segment_at'] or 'none'} to {impact['newest_segment_at'] or 'none'}\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
