"""录像压缩校验留痕的唯一严格解码; 候选消费和实际素材代次共用。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from edge_runtime.configuration_values import (
    _crf_value,
    _non_empty_string,
    _non_negative_integer,
    _object,
    _positive_integer,
    _positive_number,
    _require_keys,
)

MATERIAL_GENERATION_COMPRESSED = "compressed"


@dataclass(frozen=True, slots=True)
class RecordingCompressionCandidate:
    """已通过真实校验的候选及其编码参数/代次留痕; S048 消费的同一产物。"""

    source: str
    candidate: str
    source_sha256: str
    candidate_sha256: str
    source_size_bytes: int
    candidate_size_bytes: int
    source_duration_seconds: float
    candidate_duration_seconds: float
    source_frames: int
    candidate_frames: int
    candidate_mtime_ns: int
    crf: int
    encoder: str
    preset: str
    generation: str
    encoded_at: str

    def to_wire(self) -> dict[str, object]:
        """转换为本机留痕文档; 文档字段名就是这里的字段名, 只在此处定义一次。"""
        return asdict(self)


_PROVENANCE_KEYS = frozenset(RecordingCompressionCandidate.__dataclass_fields__)


def read_compression_provenance(path: Path) -> RecordingCompressionCandidate:
    """严格读取留痕; 字段集合或类型不符即拒绝, 不把无法证明的候选当已验证。"""
    document = _object(json.loads(path.read_text(encoding="utf-8")), "compression provenance")
    _require_keys(document, required=_PROVENANCE_KEYS)
    generation = _non_empty_string(document["generation"], "provenance generation")
    if generation != MATERIAL_GENERATION_COMPRESSED:
        raise ValueError("compression provenance generation must be compressed")
    return RecordingCompressionCandidate(
        source=_non_empty_string(document["source"], "provenance source"),
        candidate=_non_empty_string(document["candidate"], "provenance candidate"),
        source_sha256=_non_empty_string(document["source_sha256"], "provenance source_sha256"),
        candidate_sha256=_non_empty_string(
            document["candidate_sha256"], "provenance candidate_sha256"
        ),
        source_size_bytes=_positive_integer(
            document["source_size_bytes"], "provenance source_size_bytes"
        ),
        candidate_size_bytes=_positive_integer(
            document["candidate_size_bytes"], "provenance candidate_size_bytes"
        ),
        candidate_mtime_ns=_non_negative_integer(
            document["candidate_mtime_ns"], "provenance candidate_mtime_ns"
        ),
        source_duration_seconds=_positive_number(
            document["source_duration_seconds"], "provenance source_duration_seconds"
        ),
        candidate_duration_seconds=_positive_number(
            document["candidate_duration_seconds"], "provenance candidate_duration_seconds"
        ),
        source_frames=_positive_integer(document["source_frames"], "provenance source_frames"),
        candidate_frames=_positive_integer(
            document["candidate_frames"], "provenance candidate_frames"
        ),
        crf=_crf_value(document["crf"], "compression provenance crf"),
        encoder=_non_empty_string(document["encoder"], "provenance encoder"),
        preset=_non_empty_string(document["preset"], "provenance preset"),
        generation=generation,
        encoded_at=_non_empty_string(document["encoded_at"], "provenance encoded_at"),
    )
