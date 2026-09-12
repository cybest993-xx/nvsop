"""标注基座只读数据卷的路径与身份 seam。"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

from factory_sop.dataset.adapters.annotation_volume import LocalAnnotationDataVolume
from factory_sop.dataset.annotation import AnnotationDataVolumeUnavailableError


def _volume_tree(root: Path) -> None:
    video = root / "data-1" / "video-1_source.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"source")
    folder = video.parent / video.stem
    folder.mkdir()
    (folder / f"{video.stem}_annotation.json").write_bytes(b"[]")
    (folder / "clip.mp4").write_bytes(b"clip")


def test_reads_annotation_and_fixed_clip_from_the_success_identity(tmp_path: Path) -> None:
    _volume_tree(tmp_path)
    volume = LocalAnnotationDataVolume(tmp_path)

    assert volume.read_annotation(data_id="data-1", video_id="video-1") == b"[]"
    destination = BytesIO()
    volume.read_video(
        data_id="data-1",
        video_id="video-1",
        filename="clip.mp4",
        destination=destination,
    )
    assert destination.getvalue() == b"clip"


@pytest.mark.parametrize(
    "value", ["../data-1", "data/1", "/absolute", "*", "video?.mp4", "video[0].mp4"]
)
def test_rejects_unsafe_success_identity(tmp_path: Path, value: str) -> None:
    _volume_tree(tmp_path)
    with pytest.raises(AnnotationDataVolumeUnavailableError):
        LocalAnnotationDataVolume(tmp_path).read_annotation(data_id=value, video_id="video-1")


def test_rejects_symlinked_data_directory_and_source_video(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "video-1_source.mp4").write_bytes(b"outside")
    (tmp_path / "data-link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AnnotationDataVolumeUnavailableError):
        LocalAnnotationDataVolume(tmp_path).read_annotation(data_id="data-link", video_id="video-1")

    data = tmp_path / "data-1"
    data.mkdir()
    (data / "video-1_source.mp4").symlink_to(outside / "video-1_source.mp4")
    with pytest.raises(AnnotationDataVolumeUnavailableError):
        LocalAnnotationDataVolume(tmp_path).read_annotation(data_id="data-1", video_id="video-1")
